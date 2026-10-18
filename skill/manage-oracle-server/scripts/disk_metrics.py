#!/usr/bin/env python3
"""One-shot Linux filesystem metrics. Default mode samples locally without OCI."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import http.client
import json
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys

METRICS = ("DiskAvailableBytes", "DiskUsedPercent", "InodeUsedPercent")
INGESTION_TEMPLATE = "https://telemetry-ingestion.{region}.{secondLevelDomain}"


class CollectorError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_text(value, pattern, label, maximum=512):
    if not isinstance(value, str) or len(value) > maximum or not re.fullmatch(pattern, value):
        raise CollectorError("invalid_config", f"{label} 无效或仍为占位符。")
    return value


def validate_config(config):
    if not isinstance(config, dict) or type(config.get("version")) is not int or config["version"] != 1:
        raise CollectorError("invalid_config", "采集配置 version 必须是整数 1。")
    _valid_text(config.get("instance_id"), r"ocid1\.instance\.[A-Za-z0-9._-]+", "instance_id")
    _valid_text(config.get("compartment_id"), r"ocid1\.(?:compartment|tenancy)\.[A-Za-z0-9._-]+", "compartment_id")
    _valid_text(config.get("region"), r"[a-z][a-z0-9-]{1,62}", "region")
    namespace = _valid_text(config.get("namespace"), r"[A-Za-z][A-Za-z0-9_]{0,99}", "namespace")
    if namespace.lower().startswith(("oci_", "oracle_")):
        raise CollectorError("reserved_namespace", "自定义 namespace 不能使用 oci_ 或 oracle_ 前缀。")
    mounts = config.get("mountpoints")
    if not isinstance(mounts, list) or not 1 <= len(mounts) <= 16:
        raise CollectorError("invalid_mountpoints", "需要 1–16 个明确挂载点，以保持每次最多 48 个指标流。")
    if len(set(str(p) for p in mounts)) != len(mounts):
        raise CollectorError("invalid_mountpoints", "挂载点不能重复。")
    for mount in mounts:
        if not isinstance(mount, str) or not mount.startswith("/") or "\x00" in mount or any(ord(c) < 32 for c in mount):
            raise CollectorError("invalid_mountpoints", "挂载点必须是 Linux 绝对路径。")
        if posixpath.normpath(mount) != mount or any(p == ".." for p in PurePosixPath(mount).parts):
            raise CollectorError("invalid_mountpoints", "挂载点使用规范化路径，不接受路径穿越或末尾斜线。")
    return config


def read_config(path):
    p = Path(path)
    if not p.is_absolute():
        raise CollectorError("invalid_path", "配置必须使用包外绝对路径。")
    try:
        p = p.resolve()
        package = Path(__file__).resolve().parents[1]
        if p == package or package in p.parents or any((q / ".git").exists() for q in p.parents):
            raise CollectorError("unsafe_storage", "采集配置必须位于程序包和 Git 工作区之外。")
        if not p.is_file() or p.stat().st_size > 65536:
            raise CollectorError("invalid_config", "无法读取大小合理的采集配置文件。")
        config = json.loads(p.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_fields)
    except (OSError, ValueError, UnicodeError):
        raise CollectorError("invalid_config", "采集配置不是可读取的 UTF-8 JSON。") from None
    return validate_config(config)


def _unique_fields(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise CollectorError("duplicate_key", "采集配置含重复字段，不能确定批准范围。")
        output[key] = value
    return output


def scope(config):
    return {key: config[key] for key in ("instance_id", "compartment_id", "region", "namespace", "mountpoints")} | {
        "metrics": list(METRICS), "operation": "publish-custom-filesystem-metrics", "schedule_seconds": 60
    }


def scope_hash(config):
    raw = json.dumps(scope(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_approval(config):
    approval = config.get("publishing_approval")
    if not isinstance(approval, dict) or approval.get("scope_sha256") != scope_hash(config):
        raise CollectorError("confirmation_required", "先展示采集范围并询问用户，再记录与该范围对应的批准；当前范围尚未确认。")
    try:
        stamp = datetime.fromisoformat(approval["confirmed_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError()
    except (KeyError, AttributeError, ValueError, TypeError):
        raise CollectorError("confirmation_required", "需要真实的、带时区的首次发布批准时间。") from None


def filesystem_values(stat):
    if stat.f_blocks <= 0 or stat.f_frsize <= 0:
        raise CollectorError("unsupported_filesystem", "文件系统未返回有效块容量。")
    available = max(0, stat.f_bavail) * stat.f_frsize
    used_blocks = max(0, stat.f_blocks - stat.f_bfree)
    usable_blocks = used_blocks + max(0, stat.f_bavail)
    used_percent = 100.0 * used_blocks / usable_blocks if usable_blocks > 0 else 0.0
    values = {"DiskAvailableBytes": available, "DiskUsedPercent": min(100.0, max(0.0, used_percent))}
    if stat.f_files > 0 and stat.f_ffree >= 0:
        values["InodeUsedPercent"] = min(100.0, max(0.0, 100.0 * (stat.f_files - stat.f_ffree) / stat.f_files))
    if not all(math.isfinite(value) for value in values.values()):
        raise CollectorError("invalid_sample", "文件系统采样值非有限数值。")
    return values


def collect(config, statvfs=None, ismount=None, now=None):
    validate_config(config)
    if statvfs is None:
        if not hasattr(os, "statvfs"):
            raise CollectorError("unsupported_os", "采集器需要在目标 Linux 实例运行；本机 Windows 仅运行 OCI 管理入口。")
        statvfs = os.statvfs
    ismount = ismount or os.path.ismount
    timestamp = now or utc_now()
    data, errors, notes = [], [], []
    for mount in config["mountpoints"]:
        try:
            if os.path.realpath(mount) != mount:
                raise CollectorError("mount_symlink", "挂载点必须使用已核对的真实路径，不能经过符号链接。")
            if not ismount(mount):
                raise CollectorError("not_mounted", "配置路径当前不是挂载点；未退回采集父文件系统。")
            values = filesystem_values(statvfs(mount))
            if "InodeUsedPercent" not in values:
                notes.append({"mountpoint": mount, "code": "inode_unavailable"})
            for name, value in values.items():
                data.append({
                    "compartment_id": config["compartment_id"], "namespace": config["namespace"],
                    "name": name, "dimensions": {"resourceId": config["instance_id"], "mountpoint": mount},
                    "metadata": {"unit": "bytes" if name == "DiskAvailableBytes" else "percent"},
                    "datapoints": [{"timestamp": timestamp, "value": value}]
                })
        except CollectorError as exc:
            errors.append({"mountpoint": mount, "code": exc.code, "message": exc.message})
        except OSError:
            errors.append({"mountpoint": mount, "code": "filesystem_read_failed", "message": "无法读取文件系统状态。"})
    return {"status": "partial" if data and errors else "ok" if data else "error", "observed_at": timestamp,
            "metric_data": data, "errors": errors, "notes": notes}


def make_request(sdk, metric_data):
    models = sdk.monitoring.models
    metrics = []
    for datum in metric_data:
        points = [models.Datapoint(timestamp=datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")), value=p["value"]) for p in datum["datapoints"]]
        metrics.append(models.MetricDataDetails(
            compartment_id=datum["compartment_id"], namespace=datum["namespace"], name=datum["name"],
            dimensions=datum["dimensions"], metadata=datum["metadata"], datapoints=points))
    return models.PostMetricDataDetails(metric_data=metrics, batch_atomicity="ATOMIC")


def ingestion_endpoint(sdk, region):
    # telemetry-ingestion is an endpoint prefix, not a registered SDK service.
    return sdk.regions.endpoint_for("monitoring", region=region,
                                    service_endpoint_template=INGESTION_TEMPLATE)


def check_publish_response(response):
    payload = response.data
    count = getattr(payload, "failed_metrics_count", None)
    failures = getattr(payload, "failed_metrics", None)
    if type(count) is not int or count < 0 or (failures is not None and not isinstance(failures, list)):
        raise CollectorError("unverified_publish", "上报响应缺少失败统计，不能宣称发布成功。")
    if count != 0 or failures:
        raise CollectorError("metric_publish_failed", "OCI 返回指标发布失败；按原目标排查，不输出包含数据的原始错误正文。")
    return {"failed_metrics_count": 0}


def validate_payload(config, metric_data):
    """Reject samples outside the exact approved target and metric set."""
    if not isinstance(metric_data, list) or not 1 <= len(metric_data) <= 48:
        raise CollectorError("invalid_payload", "需要 1–48 个已确认范围内的指标流。")
    seen = set()
    for datum in metric_data:
        if not isinstance(datum, dict) or set(datum) != {"compartment_id", "namespace", "name", "dimensions", "metadata", "datapoints"}:
            raise CollectorError("invalid_payload", "指标数据字段不符合采集器的固定格式。")
        dimensions = datum["dimensions"]
        if (datum["compartment_id"] != config["compartment_id"] or datum["namespace"] != config["namespace"]
                or datum["name"] not in METRICS or not isinstance(dimensions, dict)
                or set(dimensions) != {"resourceId", "mountpoint"}
                or dimensions["resourceId"] != config["instance_id"] or dimensions["mountpoint"] not in config["mountpoints"]):
            raise CollectorError("scope_mismatch", "指标数据超出用户确认的目标、namespace、指标或挂载点范围。")
        identity = (datum["name"], dimensions["mountpoint"])
        if identity in seen:
            raise CollectorError("invalid_payload", "同一采样不能重复发布相同指标流。")
        seen.add(identity)
        expected_unit = "bytes" if datum["name"] == "DiskAvailableBytes" else "percent"
        if datum["metadata"] != {"unit": expected_unit}:
            raise CollectorError("invalid_payload", "指标单位不符合固定格式。")
        points = datum["datapoints"]
        if not isinstance(points, list) or len(points) != 1 or not isinstance(points[0], dict) or set(points[0]) != {"timestamp", "value"}:
            raise CollectorError("invalid_payload", "每个流只能包含本次采集的一个数据点。")
        number = points[0]["value"]
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0 or (expected_unit == "percent" and number > 100):
            raise CollectorError("invalid_payload", "指标值不是有效的容量或百分比。")
        try:
            stamp = datetime.fromisoformat(points[0]["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not datetime.now(timezone.utc) - timedelta(minutes=5) <= stamp <= datetime.now(timezone.utc) + timedelta(minutes=1):
                raise ValueError()
        except (AttributeError, ValueError, TypeError):
            raise CollectorError("invalid_payload", "数据点必须来自本次近期采样并使用带时区时间。") from None


@contextmanager
def direct_metadata():
    """Bypass environment/system HTTP proxies for SDK metadata access only."""
    keys = ("no_proxy", "NO_PROXY")
    previous = {key: os.environ.get(key) for key in keys}
    bypass = ",".join(value for value in previous.values() if value)
    bypass += ",169.254.169.254,fd00:c1::a9fe:a9fe,[fd00:c1::a9fe:a9fe]"
    try:
        for key in keys:
            os.environ[key] = bypass.lstrip(",")
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def verify_instance_identity(expected, connection_factory=http.client.HTTPConnection):
    """Read only the fixed IMDSv2 identity field; no proxies or redirects."""
    connection = connection_factory("169.254.169.254", timeout=3)
    try:
        connection.request("GET", "/opc/v2/instance/id", headers={"Authorization": "Bearer Oracle"})
        response = connection.getresponse()
        if response.status != 200:
            raise CollectorError("instance_identity_unavailable", "无法通过 IMDSv2 核验当前实例身份；停止上报，不回退到旧元数据接口。")
        raw = response.read(1025)
        if len(raw) > 1024:
            raise CollectorError("instance_identity_unavailable", "实例身份响应超出预期；停止上报。")
        identifier = raw.decode("utf-8").strip()
        if identifier.startswith('"'):
            identifier = json.loads(identifier)
        if identifier != expected:
            raise CollectorError("instance_mismatch", "当前实例与已确认的采集目标不同；停止上报，不能给另一实例记账。")
    except CollectorError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise CollectorError("instance_identity_unavailable", "无法通过固定 IMDSv2 地址核验实例身份；检查实例元数据可达性。") from None
    finally:
        connection.close()


def publish(config, metric_data, sdk=None):
    validate_config(config)
    validate_approval(config)
    validate_payload(config, metric_data)
    if sdk is None:
        try:
            import oci as sdk
        except ImportError:
            raise CollectorError("missing_sdk", "采集器的独立 Python 环境中尚未安装官方 oci 依赖。") from None
    try:
        if os.environ.get("OCI_METADATA_BASE_URL"):
            raise CollectorError("metadata_override", "采集器不接受自定义元数据地址；请使用实例原生身份端点。")
        with direct_metadata():
            verify_instance_identity(config["instance_id"])
            signer = sdk.auth.signers.InstancePrincipalsSecurityTokenSigner(
                retry_strategy=sdk.retry.NoneRetryStrategy(),
                federation_client_retry_strategy=sdk.retry.NoneRetryStrategy(), log_requests=False)
            signer_region = getattr(signer, "region", None)
            if signer_region != config["region"]:
                raise CollectorError("region_mismatch", "实例身份区域与已确认的指标目标区域不一致。")
            endpoint = ingestion_endpoint(sdk, config["region"])
            client = sdk.monitoring.MonitoringClient(
                {"region": config["region"]}, signer=signer, service_endpoint=endpoint,
                timeout=(5, 15), retry_strategy=sdk.retry.NoneRetryStrategy())
            response = client.post_metric_data(make_request(sdk, metric_data), retry_strategy=sdk.retry.NoneRetryStrategy())
        result = check_publish_response(response)
        request_id = (getattr(response, "headers", None) or {}).get("opc-request-id")
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9/_.:-]{1,256}", request_id):
            result["request_id"] = request_id
        return result
    except CollectorError:
        raise
    except Exception:
        raise CollectorError("oci_publish_error", "OCI 发布失败；检查实例身份、指标权限、时间和网络，未输出原始凭据或异常正文。") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description="一次性采集已配置的 Linux 文件系统；默认仅预览，不访问 OCI。")
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--describe-scope", action="store_true")
    mode.add_argument("--publish", action="store_true", help="仅用于用户已确认范围后的单次或定时发布")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        if args.describe_scope:
            print(json.dumps({"status": "planned", "scope": scope(config), "scope_sha256": scope_hash(config),
                              "requires_user_confirmation": True}, ensure_ascii=False, indent=2))
            return 0
        if args.publish:
            validate_approval(config)
        result = collect(config)
        if args.publish and result["metric_data"]:
            result["publication"] = publish(config, result["metric_data"])
            result["metric_count"] = len(result.pop("metric_data"))
        result["mode"] = "publish" if args.publish else "dry_run"
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return {"ok": 0, "partial": 3, "error": 2}[result["status"]]
    except CollectorError as exc:
        print(json.dumps({"status": "error", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        return 2
    except (OSError, TypeError, ValueError):
        print(json.dumps({"status": "error", "code": "local_error", "message": "采样失败；未输出原始路径或配置。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
