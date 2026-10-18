"""Bounded OCI SDK access and safe, allowlisted evidence helpers.

No client is constructed at import time. Credentials are consumed only by the
official SDK/signers and are never included in returned data or error messages.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import importlib
import math
from pathlib import Path

from server_config import SkillError, validate_oci, validate_private_path


MAX_PAGES = 10
PAGE_SIZE = 100
MAX_ITEMS = 1000
MAX_READ_CALLS = 200
READ_METHODS = frozenset({
    "get_instance", "list_vnic_attachments", "list_volume_attachments",
    "list_boot_volume_attachments", "get_vnic", "get_volume", "get_boot_volume",
    "get_subnet", "get_vcn", "list_private_ips", "list_ipv6s", "get_private_ip",
    "get_network_security_group", "list_network_security_group_security_rules",
    "get_security_list", "get_route_table", "get_internet_gateway",
    "get_nat_gateway", "get_service_gateway", "get_local_peering_gateway",
    "get_drg", "list_metrics", "list_alarms", "list_alarms_status", "get_alarm",
    "summarize_metrics_data", "list_topics", "get_topic", "list_subscriptions",
    "get_subscription",
})


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_error(exc):
    """Never interpolate an SDK/transport exception's message or repr."""
    if isinstance(exc, SkillError):
        result = {"code": exc.code, "message": exc.message}
        if type(getattr(exc, "http_status", None)) is int:
            result["http_status"] = exc.http_status
        return result
    status = getattr(exc, "status", None)
    messages = {
        401: ("oci_auth_failed", "OCI 身份无效或会话令牌已过期；请核对已确认的身份配置。"),
        403: ("oci_forbidden", "OCI 拒绝读取此对象；权限缺口不能解释为空资源。"),
        404: ("oci_not_authorized_or_not_found", "OCI 返回未授权或未找到；无法据此断言资源不存在。"),
        429: ("oci_throttled", "OCI 请求限流，有限重试后仍未完成。"),
    }
    if type(status) is int:
        code, message = messages.get(status, (
            "oci_service_unavailable" if status >= 500 else "oci_request_failed",
            "OCI 请求未完成；未输出可能含敏感信息的服务端异常正文。",
        ))
        return {"code": code, "message": message, "http_status": status}
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "ConnectTimeout", "ReadTimeout", "ConnectionError", "RequestException",
        "ConnectTimeoutError", "ReadTimeoutError", "CircuitBreakerError",
    }:
        return {"code": "oci_transport_error", "message": "OCI 连接、读取超时或传输失败；结果未知。"}
    return {"code": "oci_operation_failed", "message": "OCI 操作未完成；未输出底层异常或凭据。"}


def value(obj, field, default=None):
    return obj.get(field, default) if isinstance(obj, dict) else getattr(obj, field, default)


def scalar(item):
    if item is None or isinstance(item, (str, bool, int)):
        return item
    if isinstance(item, float):
        return item if math.isfinite(item) else None
    if isinstance(item, (datetime, date)):
        return item.isoformat()
    if isinstance(item, (list, tuple)) and all(
        entry is None or isinstance(entry, (str, bool, int, float)) for entry in item
    ):
        return [scalar(entry) for entry in item]
    return None


def pick(obj, *fields):
    """Select explicit scalar fields; never recursively serialize an SDK model."""
    return {field: scalar(value(obj, field)) for field in fields}


def collect_pages(method, *args, max_pages=MAX_PAGES, page_size=PAGE_SIZE,
                  max_items=MAX_ITEMS, **kwargs):
    """Follow bounded OCI page tokens, preserving results before an error.

    Returns SDK objects in ``items`` for subsequent field selection. Callers must
    propagate ``complete`` and ``errors``; incomplete lists are never empty facts.
    """
    if not (1 <= max_pages <= MAX_PAGES and 1 <= page_size <= PAGE_SIZE
            and 1 <= max_items <= MAX_ITEMS):
        raise SkillError("invalid_read_limit", "读取上限参数无效。")
    result = {"items": [], "complete": False, "pages": 0, "errors": []}
    page = None
    seen = set()
    for _ in range(max_pages):
        options = dict(kwargs, limit=page_size)
        if page is not None:
            options["page"] = page
        try:
            response = method(*args, **options)
            rows = response.data
            if not isinstance(rows, (list, tuple)):
                rows = value(rows, "items")
            if not isinstance(rows, (list, tuple)):
                raise SkillError("unexpected_response", "OCI 列表响应结构不符合预期。")
            result["pages"] += 1
            remaining = max_items - len(result["items"])
            result["items"].extend(rows[:remaining])
            if len(rows) > remaining:
                raise SkillError("pagination_limit", "列表超过项目上限；仅保留部分证据。")
            headers = response.headers or {}
            next_page = next((v for k, v in headers.items()
                              if k.lower() == "opc-next-page"), None)
            if not next_page:
                result["complete"] = True
                return result
            if not isinstance(next_page, str) or next_page in seen:
                raise SkillError("pagination_loop", "分页令牌无效或重复；已停止继续请求。")
            if len(result["items"]) >= max_items:
                raise SkillError("pagination_limit", "列表超过项目上限；仅保留部分证据。")
            seen.add(next_page)
            page = next_page
        except Exception as exc:
            result["errors"].append(safe_error(exc))
            return result
    result["errors"].append({"code": "pagination_limit", "message": "列表超过页数上限；仅保留部分证据。"})
    return result


class OciSession:
    """Use only an explicitly confirmed local profile and explicit region."""

    CLIENT_TYPES = {
        "compute": ("core", "ComputeClient"),
        "network": ("core", "VirtualNetworkClient"),
        "blockstorage": ("core", "BlockstorageClient"),
        "monitoring": ("monitoring", "MonitoringClient"),
        "notification_control": ("ons", "NotificationControlPlaneClient"),
        "notification_data": ("ons", "NotificationDataPlaneClient"),
    }

    def __init__(self, profile):
        validate_oci(profile)
        self.settings = dict(profile["oci"])
        self.instance_id = self.settings["instance_id"]
        self.compartment_id = self.settings["compartment_id"]
        self.region = self.settings["region"]
        self._clients = {}
        self._read_calls = 0
        try:
            self.sdk = importlib.import_module("oci")
        except ImportError:
            raise SkillError("oci_sdk_missing", "缺少官方 oci Python SDK。先确认独立环境的安装计划，获授权后由 Agent 安装；脚本不会自动安装。") from None
        try:
            loaded = self.sdk.config.from_file(
                file_location=self.settings["config_file"],
                profile_name=self.settings["config_profile"],
            )
            # Discard unrelated configuration such as debug/logging overrides.
            self._config = {key: loaded[key] for key in (
                "tenancy", "user", "fingerprint", "key_file", "pass_phrase"
            ) if key in loaded}
            self._config["region"] = self.region
            self._config["log_requests"] = False
            self._signer = None
            if self.settings["auth_mode"] == "security_token":
                token_path = self._credential_path(loaded.get("security_token_file"))
                key_path = self._credential_path(loaded.get("key_file"))
                with token_path.open("r", encoding="utf-8") as handle:
                    token = handle.read(65537)
                if not token.strip() or len(token) > 65536:
                    raise SkillError("invalid_security_token", "配置中的会话令牌为空或超过读取上限。")
                token = token.strip()
                private_key = self.sdk.signer.load_private_key_from_file(
                    str(key_path), pass_phrase=loaded.get("pass_phrase")
                )
                self._signer = self.sdk.auth.signers.SecurityTokenSigner(token, private_key)
            elif "key_file" in self._config:
                self._config["key_file"] = str(self._credential_path(self._config["key_file"]))
            self._retry = self.sdk.retry.RetryStrategyBuilder(
                max_attempts_check=True, max_attempts=3,
                total_elapsed_time_check=True, total_elapsed_time_seconds=30,
                retry_max_wait_between_calls_seconds=3,
                retry_base_sleep_time_seconds=1,
                service_error_check=True, service_error_retry_on_any_5xx=True,
                service_error_retry_config={429: []},
            ).get_retry_strategy()
        except SkillError:
            raise
        except Exception:
            raise SkillError("oci_auth_config_error", "无法加载已指定的 OCI 身份配置、私钥或会话令牌；未输出路径或凭据，不自动创建会话。") from None

    @staticmethod
    def _credential_path(raw):
        if not isinstance(raw, str) or not raw:
            raise SkillError("oci_credential_path_missing", "已指定的 OCI 配置缺少所需凭据文件路径。")
        # Expansion applies only to paths explicitly present in that config.
        return validate_private_path(Path(raw).expanduser(), must_exist=True)

    def client(self, name):
        if name not in self.CLIENT_TYPES:
            raise SkillError("unsupported_client", "此工具不开放所请求的 OCI 客户端。")
        if name not in self._clients:
            module, class_name = self.CLIENT_TYPES[name]
            try:
                client_type = getattr(getattr(self.sdk, module), class_name)
                options = {"timeout": (5, 15), "retry_strategy": self._retry}
                if self._signer is not None:
                    options["signer"] = self._signer
                self._clients[name] = client_type(dict(self._config), **options)
            except Exception:
                raise SkillError("oci_client_error", "无法为已确认的身份和 region 初始化 OCI 客户端。") from None
        return self._clients[name]

    def call(self, method, *args, **kwargs):
        if getattr(method, "__name__", "") not in READ_METHODS:
            raise SkillError("read_only_violation", "只读调用层不允许此 OCI 操作。")
        if self._read_calls >= MAX_READ_CALLS:
            raise SkillError("read_call_limit", "已达到本次只读请求上限；未读取的关联对象标记为不完整。")
        self._read_calls += 1
        try:
            kwargs["retry_strategy"] = self._retry
            return method(*args, **kwargs)
        except SkillError:
            raise
        except Exception as exc:
            error = safe_error(exc)
            failure = SkillError(error["code"], error["message"])
            if "http_status" in error:
                failure.http_status = error["http_status"]
            raise failure from None

    def ensure_instance(self):
        """Re-read the exact target before every operation; do not cache it."""
        response = self.call(self.client("compute").get_instance, self.instance_id)
        instance = response.data
        # Instance.region returns legacy phx/iad for these two regions.
        aliases = getattr(getattr(self.sdk, "regions", None), "REGIONS_SHORT_NAMES",
                          {"phx": "us-phoenix-1", "iad": "us-ashburn-1"})
        observed_region = value(instance, "region")
        observed_region = aliases.get(observed_region, observed_region)
        expected_region = aliases.get(self.region, self.region)
        if (value(instance, "id") != self.instance_id
                or value(instance, "compartment_id") != self.compartment_id
                or observed_region != expected_region):
            raise SkillError("target_mismatch", "OCI 实例身份、compartment 或 region 与已确认目标不一致，已停止。")
        return instance
