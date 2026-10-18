#!/usr/bin/env python3
"""Narrow OCI alarm plan/apply workflow; no delete or arbitrary-MQL entry point.

The SHA256 binds an explicit approval to reviewed content. It is not an
authorization service or a sandbox: the agent must ask the user about the
concrete plan before passing --approve-plan, for both create and update.

API fields and write kwargs:
https://docs.oracle.com/en-us/iaas/tools/python/latest/api/monitoring/client/oci.monitoring.MonitoringClient.html
https://docs.oracle.com/en-us/iaas/tools/python/latest/api/monitoring/models/oci.monitoring.models.CreateAlarmDetails.html
Absence detection is separate from pending_duration:
https://docs.oracle.com/en-us/iaas/Content/Monitoring/Tasks/create-edit-alarm-query-absence-detection-period.htm
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid

from server_config import SkillError, validate_private_path


PLAN_TTL = timedelta(minutes=15)
MAX_JSON_BYTES = 128 * 1024
MAX_LIST_PAGES = 25
PLAN_TAG = "manage-oracle-server-plan"
NOTICE = (
    "先向用户展示此具体计划并取得针对本次创建或更新的明确确认，之后才能传入 "
    "--approve-plan。一般任务授权不能替代此确认；SHA256 只绑定内容，不证明用户授权。"
)
SPEC_FIELDS = {
    "kind", "namespace", "metric", "statistic", "threshold", "comparison",
    "pending_duration", "absence_detection_period", "mountpoint", "compartment_id",
    "metric_compartment_id", "topic_id", "display_name", "description", "body",
    "severity", "is_enabled", "alarm_id",
}
CORE_FIELDS = (
    "compartment_id", "display_name", "metric_compartment_id",
    "metric_compartment_id_in_subtree", "namespace", "query", "resolution",
    "pending_duration", "severity", "body", "alarm_summary", "destinations",
    "is_enabled", "is_notifications_per_metric_dimension_enabled", "message_format",
)
PRESERVED_FIELDS = (
    "resource_group", "repeat_notification_duration", "suppression", "overrides",
    "rule_name", "notification_version", "notification_title",
    "evaluation_slack_duration", "freeform_tags", "defined_tags",
)
TEXT_DIGEST_FIELDS = {"body", "alarm_summary"}
SECRET_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE)-----|\bBearer\s+\S+|"
    r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|security[_-]?token)\s*[:=]\s*\S+|"
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.IGNORECASE,
)


def _fail(code, message):
    raise SkillError(code, message) from None


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value, label, maximum=256, empty=False):
    if (not isinstance(value, str) or (not empty and not value) or len(value) > maximum
            or any(unicodedata.category(c).startswith("C") for c in value)):
        _fail("invalid_alarm_spec", f"{label} 必须是长度不超过 {maximum} 的文本，不能含控制字符。")
    if SECRET_PATTERN.search(value):
        _fail("secret_in_alarm_text", f"{label} 疑似含凭据；告警文字会进入云端，请移除敏感内容。")
    return value


def _ocid(value, kinds, label):
    if (not isinstance(value, str) or len(value) > 512 or not re.fullmatch(
            r"ocid1\.(?:" + "|".join(kinds) + r")\.[A-Za-z0-9._-]+", value)):
        _fail("invalid_alarm_spec", f"{label} 需要正确资源类型的完整 OCID。")
    return value


def _pending(value):
    match = re.fullmatch(r"PT(?:(\d{1,4})H)?(?:(\d{1,4})M)?", value) if isinstance(value, str) else None
    minutes = int(match[1] or 0) * 60 + int(match[2] or 0) if match else 0
    if not 1 <= minutes <= 60:
        _fail("invalid_alarm_spec", "pending_duration 仅支持 ISO 8601 整分钟 PT1M–PT1H。")
    return f"PT{minutes}M", minutes


def _absence(value):
    match = re.fullmatch(r"([1-9]\d{0,4})([mhd])", value) if isinstance(value, str) else None
    minutes = int(match[1]) * {"m": 1, "h": 60, "d": 1440}[match[2]] if match else 0
    if not 1 <= minutes <= 4320:
        _fail("invalid_alarm_spec", "absence_detection_period 需要 1m–72h 的 m/h/d 时长。")
    return f"{minutes}m", minutes


def normalize_spec(raw):
    """Validate a small declarative schema, rejecting auth, MQL and unknown fields."""
    if not isinstance(raw, dict) or set(raw) - SPEC_FIELDS:
        _fail("invalid_alarm_spec", "告警 spec 必须是受支持的对象；不接受未知字段或任意 query。")
    required = {"kind", "namespace", "metric", "pending_duration", "compartment_id",
                "metric_compartment_id", "topic_id", "display_name"}
    if required - set(raw) or not isinstance(raw["kind"], str) or raw["kind"] not in {"threshold", "absence"}:
        _fail("invalid_alarm_spec", "缺少必需告警字段，或 kind 不是 threshold/absence。")
    spec = {key: raw[key] for key in required}
    for key in ("namespace", "metric"):
        if not isinstance(spec[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", spec[key]):
            _fail("invalid_alarm_spec", f"{key} 仅支持字母开头的字母、数字及下划线名称。")
    spec["display_name"] = _text(raw["display_name"], "display_name")
    for key in ("description", "body"):
        spec[key] = _text(raw.get(key, ""), key, maximum=1024, empty=True)
    for key in ("compartment_id", "metric_compartment_id"):
        spec[key] = _ocid(raw[key], ("compartment", "tenancy"), key)
    spec["topic_id"] = _ocid(raw["topic_id"], ("onstopic",), "topic_id")
    spec["pending_duration"], pending_minutes = _pending(raw["pending_duration"])
    spec["severity"] = raw.get("severity", "WARNING")
    if not isinstance(spec["severity"], str) or spec["severity"] not in {"CRITICAL", "ERROR", "WARNING", "INFO"}:
        _fail("invalid_alarm_spec", "severity 必须为 CRITICAL、ERROR、WARNING 或 INFO。")
    spec["is_enabled"] = raw.get("is_enabled", True)
    if type(spec["is_enabled"]) is not bool:
        _fail("invalid_alarm_spec", "is_enabled 必须是 JSON 布尔值。")
    if "mountpoint" in raw:
        mount = raw["mountpoint"]
        if not isinstance(mount, str) or len(mount) > 512 or not re.fullmatch(r"/[A-Za-z0-9._/-]*", mount):
            _fail("invalid_alarm_spec", "mountpoint 需要绝对 Linux 挂载点；本入口不支持空格、引号或转义字符。")
        spec["mountpoint"] = mount
    if "alarm_id" in raw:
        spec["alarm_id"] = _ocid(raw["alarm_id"], ("alarm",), "alarm_id")
    if spec["kind"] == "threshold":
        if not {"statistic", "comparison", "threshold"} <= set(raw) or "absence_detection_period" in raw:
            _fail("invalid_alarm_spec", "threshold 需要 statistic/comparison/threshold，不能同时指定缺失检测期。")
        if (not isinstance(raw["statistic"], str) or raw["statistic"] not in {"mean", "max", "min"}
                or not isinstance(raw["comparison"], str) or raw["comparison"] not in {">", ">=", "<", "<=", "==", "!="}):
            _fail("invalid_alarm_spec", "阈值统计仅支持 mean/max/min；comparison 必须是基本比较运算符。")
        threshold = raw["threshold"]
        if type(threshold) not in (int, float) or abs(threshold) > 1e15 or not math.isfinite(threshold):
            _fail("invalid_alarm_spec", "threshold 必须是绝对值不超过 1e15 的有限 JSON 数字。")
        spec.update(statistic=raw["statistic"], comparison=raw["comparison"], threshold=threshold)
    else:
        if {"statistic", "comparison", "threshold"} & set(raw):
            _fail("invalid_alarm_spec", "absence 不能混用 threshold/statistic/comparison。")
        spec["absence_detection_period"], absence_minutes = _absence(raw.get("absence_detection_period"))
        if absence_minutes < pending_minutes:
            _fail("invalid_alarm_spec", "缺失检测期不能短于 pending_duration，否则条件可能在触发前停止产生。")
    return spec


def build_query(spec, instance_id):
    """Resource identity always comes from the confirmed server binding."""
    _ocid(instance_id, ("instance",), "绑定的 instance_id")
    dimensions = 'resourceId = ' + json.dumps(instance_id)
    grouping = "resourceId"
    if "mountpoint" in spec:
        dimensions += ", mountpoint = " + json.dumps(spec["mountpoint"])
        grouping += ", mountpoint"
    query = f'{spec["metric"]}[1m]{{{dimensions}}}.groupBy({grouping})'
    if spec["kind"] == "absence":
        return query + f'.absent({spec["absence_detection_period"]})'
    threshold = json.dumps(spec["threshold"], allow_nan=False)
    return query + f'.{spec["statistic"]}() {spec["comparison"]} {threshold}'


def _desired(spec, instance_id, plan_id):
    desired = {
        "compartment_id": spec["compartment_id"], "display_name": spec["display_name"],
        "metric_compartment_id": spec["metric_compartment_id"],
        "metric_compartment_id_in_subtree": False, "namespace": spec["namespace"],
        "query": build_query(spec, instance_id), "resolution": "1m",
        "pending_duration": spec["pending_duration"], "severity": spec["severity"],
        "body": spec["body"], "alarm_summary": spec["description"],
        "destinations": [spec["topic_id"]], "is_enabled": spec["is_enabled"],
        "is_notifications_per_metric_dimension_enabled": True, "message_format": "ONS_OPTIMIZED",
    }
    if "alarm_id" not in spec:
        desired["freeform_tags"] = {PLAN_TAG: plan_id}
    return desired


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _jsonable(value):
    """Only used to hash preserved settings; never print raw tags or SDK models."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    fields = getattr(value, "swagger_types", None)
    if isinstance(fields, dict):
        return {key: _jsonable(getattr(value, key, None)) for key in fields}
    _fail("unsupported_alarm_state", "告警包含无法可靠核验的设置类型；未输出原始内容。")


def _field_value(obj, key):
    value = _get(obj, key)
    defaults = {"body": "", "alarm_summary": "", "resolution": "1m", "pending_duration": "PT1M",
                "metric_compartment_id_in_subtree": False,
                "is_notifications_per_metric_dimension_enabled": False, "message_format": "RAW"}
    if value is None and key in defaults:
        value = defaults[key]
    if key == "pending_duration":
        value = _pending(value)[0]
    if key == "query" and isinstance(value, str):
        value = re.sub(r"\s+", "", value)
    if key == "destinations" and isinstance(value, list):
        value = sorted(value)
    return _digest(value) if key in TEXT_DIGEST_FIELDS else value


def _etag(response, required=True):
    headers = getattr(response, "headers", {}) or {}
    value = next((v for k, v in headers.items() if k.lower() == "etag"), None)
    value = value or _get(getattr(response, "data", None), "etag")
    if required and (not isinstance(value, str) or not value or len(value) > 512):
        _fail("missing_etag", "OCI 未返回可用于并发核验的 etag；不能形成或执行告警变更。")
    return value


def _http_status(exc):
    status = getattr(exc, "status", None)
    return status if type(status) is int and 100 <= status <= 599 else None


def _read(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except SkillError:
        raise
    except Exception as exc:
        suffix = f"（HTTP {_http_status(exc)}）" if _http_status(exc) else ""
        _fail("oci_read_failed", f"OCI 只读核验失败{suffix}；未执行后续变更，未输出原始 API 错误。")


def _target(args, session):
    instance = session.ensure_instance()
    identifier = _ocid(_get(instance, "id"), ("instance",), "实例身份")
    compartment = _ocid(_get(instance, "compartment_id"), ("compartment", "tenancy"), "实例 compartment")
    if identifier != session.instance_id or compartment != session.compartment_id:
        _fail("target_mismatch", "实时实例与已确认配置绑定不一致。")
    state = _get(instance, "lifecycle_state")
    if state in {None, "TERMINATING", "TERMINATED", "UNKNOWN_ENUM_VALUE"}:
        _fail("invalid_target_state", "实例状态不适合此告警变更；先核验目标。")
    if not isinstance(args.server, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.server):
        _fail("invalid_server", "告警计划需要明确的已配置实例别名。")
    return {"server": args.server, "region": session.region, "instance_id": identifier,
            "instance_compartment_id": compartment, "lifecycle_state": state}


def _topic(session, topic_id):
    response = _read(session.client("notification_control").get_topic, topic_id)
    topic = response.data
    if _get(topic, "topic_id") != topic_id or _get(topic, "lifecycle_state") != "ACTIVE":
        _fail("invalid_topic", "指定 Notifications topic 不存在于预期身份或不是 ACTIVE。")
    return {"topic_id": topic_id, "compartment_id": _ocid(_get(topic, "compartment_id"),
            ("compartment", "tenancy"), "topic compartment"),
            "name": _text(_get(topic, "name"), "topic name"), "lifecycle_state": "ACTIVE",
            "etag": _etag(response)}


def _same_name(client, compartment_id, display_name):
    """Use exact API filtering, recheck it locally, and require complete pagination."""
    found = {}
    for state in ("ACTIVE", "DELETING"):
        page, seen = None, set()
        for _ in range(MAX_LIST_PAGES):
            kwargs = {"display_name": display_name, "lifecycle_state": state, "limit": 100}
            if page:
                kwargs["page"] = page
            response = _read(client.list_alarms, compartment_id, **kwargs)
            if not isinstance(response.data, list):
                _fail("incomplete_alarm_list", "告警列表格式异常，不能确认同名资源是否存在。")
            for alarm in response.data:
                if _get(alarm, "display_name") == display_name and _get(alarm, "compartment_id") == compartment_id:
                    identifier = _ocid(_get(alarm, "id"), ("alarm",), "已有 alarm_id")
                    found[identifier] = _get(alarm, "lifecycle_state")
            page = next((v for k, v in (response.headers or {}).items() if k.lower() == "opc-next-page"), None)
            if not page:
                break
            if page in seen:
                _fail("incomplete_alarm_list", "告警分页游标重复；不能把部分列表当作完整结果。")
            seen.add(page)
        else:
            _fail("incomplete_alarm_list", "告警列表超过有界分页范围；请缩小查询范围或按 OCID 核验。")
    return found


def _bound_query(query, instance_id):
    if not isinstance(query, str) or len(query) > 4096:
        return False
    query = re.sub(r"\s+", "", query)
    pattern = (
        r'[A-Za-z][A-Za-z0-9_]{0,127}\[\d+[mhd]\]\{resourceId="' + re.escape(instance_id)
        + r'"(?:,mountpoint="/[A-Za-z0-9._/-]*")?\}'
        + r'(?:\.groupBy\(resourceId(?:,mountpoint)?\))?\.'
        + r'(?:(?:mean|max|min)\(\)(?:>=|<=|==|!=|>|<)-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|absent\((?:\d+[mhd])?\))'
    )
    return re.fullmatch(pattern, query) is not None


def _alarm_observation(response, instance_id):
    alarm = response.data
    _text(_get(alarm, "display_name"), "已有告警 display_name")
    if _get(alarm, "lifecycle_state") != "ACTIVE":
        _fail("invalid_alarm_state", "目标告警不是 ACTIVE，不能安全更新或确认写入成功。")
    if not _bound_query(_get(alarm, "query"), instance_id):
        _fail("unrelated_alarm", "目标告警不是仅绑定当前实例的受支持简单查询；不能覆盖无关或复合告警。")
    if _get(alarm, "overrides") or _get(alarm, "resource_group") or _get(alarm, "suppression"):
        _fail("unsupported_alarm_state", "目标告警带有 overrides、resource group 或 suppression；本入口不改写这些复杂告警。")
    fields = {key: _field_value(alarm, key) for key in CORE_FIELDS}
    # Never persist old notification text, arbitrary tags or other opaque SDK data.
    preserved = _digest({key: _jsonable(_get(alarm, key)) for key in PRESERVED_FIELDS})
    marker = (_get(alarm, "freeform_tags") or {}).get(PLAN_TAG)
    if not isinstance(marker, str) or not re.fullmatch(r"[0-9a-f]{32}", marker):
        marker = None
    return {"alarm_id": _ocid(_get(alarm, "id"), ("alarm",), "alarm_id"), "etag": _etag(response),
            "lifecycle_state": "ACTIVE", "fields": fields, "preserved_settings_sha256": preserved,
            "operation_marker": marker}


def _get_alarm(client, alarm_id, instance_id, missing_ok=False):
    try:
        response = client.get_alarm(alarm_id)
    except Exception as exc:
        if missing_ok and _http_status(exc) == 404:
            return None
        _fail("oci_read_failed", "无法读取目标告警；未输出原始 API 返回内容。")
    observed = _alarm_observation(response, instance_id)
    if observed["alarm_id"] != alarm_id:
        _fail("alarm_identity_mismatch", "OCI 返回的告警身份与请求不一致。")
    return observed


def _matches(plan, observed):
    if observed is None or any(observed["fields"].get(k) != _field_value(plan["desired"], k) for k in CORE_FIELDS):
        return False
    if plan["operation"] == "create":
        return observed["operation_marker"] == plan["plan_id"]
    return (observed["alarm_id"] == plan["spec"]["alarm_id"] and
            observed["preserved_settings_sha256"] == plan["observed"]["alarm"]["preserved_settings_sha256"])


def _review(plan):
    spec, target, desired = plan["spec"], plan["target"], plan["desired"]
    before = (plan["observed"]["alarm"] or {}).get("fields", {})
    changes = [{"field": key, "before": before.get(key), "after": desired[key]}
               for key in CORE_FIELDS if before.get(key) != _field_value(desired, key)]
    return {
        "operation": plan["operation"], "server": target["server"], "region": target["region"],
        "alarm_id": spec.get("alarm_id"), "display_name": spec["display_name"],
        "namespace": spec["namespace"], "metric": spec["metric"], "resourceId": target["instance_id"],
        "mountpoint": spec.get("mountpoint"), "interval": "1m", "kind": spec["kind"],
        "statistic": spec.get("statistic"), "comparison": spec.get("comparison"), "threshold": spec.get("threshold"),
        "pending_duration": spec["pending_duration"], "absence_detection_period": spec.get("absence_detection_period"),
        "trigger_timing": (
            "按 1m 窗口计算缺失，pending_duration 决定触发等待；absence_detection_period 是持续缺失后停止产生判定值的范围，不能当作触发延迟。"
            if spec["kind"] == "absence" else
            "按 1m 窗口计算阈值，条件须持续达到 pending_duration；PT1M 在首次满足条件的评估时触发，通知投递可能另有延迟。"
        ),
        "query": desired["query"], "alarm_compartment_id": desired["compartment_id"],
        "metric_compartment_id": desired["metric_compartment_id"],
        "instance_compartment_id": target["instance_compartment_id"], "topic": plan["observed"]["topic"],
        "is_enabled": desired["is_enabled"], "severity": desired["severity"],
        "changes": changes, "old_text_values_are_sha256": sorted(TEXT_DIGEST_FIELDS),
        "impact": [
            "创建一个新告警并添加本次 operation ID 标签。" if plan["operation"] == "create" else
            "更新指定 alarm_id 的所列字段；已有非受管设置与标签保持原值，并核验其摘要。",
            "告警启用后若条件已满足，可能立即开始向指定 topic 发通知；更新目的地会改变通知去向。",
            "Alarm、metric 与 topic compartment 分别列出；跨 compartment 需要对应 IAM 权限。",
            "Monitoring/Notifications 使用可能计费；此计划不估算价格。",
            "本步骤不证明 metric 正在上报、topic 有已确认订阅或通知能够成功投递。",
        ], "confirmation_required": NOTICE,
    }


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("invalid_alarm_json", "JSON 含重复字段，不能可靠确定计划内容。")
        result[key] = value
    return result


def _read_json(path):
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            _fail("invalid_alarm_json", "告警 JSON 过大。")
        def reject_constant(_):
            _fail("invalid_alarm_json", "JSON 不能包含 NaN 或 Infinity。")
        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                reject_constant(value)
            return number
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_no_duplicates,
                          parse_constant=reject_constant, parse_float=finite_float)
    except (OSError, UnicodeError, ValueError, RecursionError):
        _fail("invalid_alarm_json", "无法读取有效告警 JSON；未输出文件内容。")


def _write_json(path, data, exclusive=False):
    """Private file creation and atomic receipts work on POSIX and Windows."""
    path = validate_private_path(path)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if exclusive:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        else:
            fd, temporary = tempfile.mkstemp(prefix=".alarm-receipt-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if temporary:
            os.replace(temporary, path)
    except FileExistsError:
        _fail("alarm_plan_exists", "输出计划已存在；请保留其 journal，使用新的私有文件名生成新计划。")
    except (OSError, ValueError):
        _fail("alarm_local_state_failed", "无法安全写入私有计划/执行记录；不要删除执行记录后盲目重建告警。")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _load_plan(path, approval):
    document = _read_json(path)
    if not isinstance(document, dict) or set(document) != {"schema_version", "sha256", "plan"} or type(document["schema_version"]) is not int or document["schema_version"] != 1:
        _fail("invalid_alarm_plan", "计划格式或版本不受支持；不能执行。")
    plan = document["plan"]
    if not isinstance(document["sha256"], str) or not hmac.compare_digest(_digest(plan), document["sha256"]) or not hmac.compare_digest(document["sha256"], approval):
        _fail("alarm_plan_hash_mismatch", "计划内容或批准的 SHA256 已变化；需重新生成、展示并确认。")
    expected = {"plan_id", "operation", "created_at", "expires_at", "target", "spec", "desired", "observed", "review", "confirmation_required"}
    if not isinstance(plan, dict) or set(plan) != expected or plan["confirmation_required"] != NOTICE:
        _fail("invalid_alarm_plan", "计划结构不受支持。")
    if not isinstance(plan["plan_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", plan["plan_id"]):
        _fail("invalid_alarm_plan", "计划 operation ID 无效。")
    try:
        created = datetime.fromisoformat(plan["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(plan["expires_at"].replace("Z", "+00:00"))
        if created.tzinfo is None or expires.tzinfo is None or expires - created != PLAN_TTL or created > _now() + timedelta(seconds=30):
            raise ValueError()
        if _now() >= expires:
            _fail("alarm_plan_expired", "计划已超过 15 分钟有效期；不能执行。若已有 started/uncertain 记录，先只读核验云端，不能直接重新创建。")
        spec = normalize_spec(plan["spec"])
        operation = "update" if "alarm_id" in spec else "create"
        if (spec != plan["spec"] or operation != plan["operation"]
                or _desired(spec, plan["target"]["instance_id"], plan["plan_id"]) != plan["desired"]
                or _review(plan) != plan["review"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("invalid_alarm_plan", "计划与受控 spec、绑定目标或供确认的展示内容不一致。")
    return plan


@contextmanager
def _apply_lock(plan_path, digest):
    lock = validate_private_path(str(plan_path) + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _fail("alarm_apply_locked", "此计划存在执行锁。先确认原进程已退出并核验 journal 与云端状态，再人工移除残留锁；不会按锁龄自动重试或删除锁。")
    except OSError:
        _fail("alarm_local_state_failed", "无法创建排他执行锁；未执行云端变更。")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": _iso(_now()), "plan_sha256": digest}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass  # A remaining lock blocks future writes and requires explicit inspection.


def plan_alarm(args, session):
    spec_path = validate_private_path(args.spec, must_exist=True)
    out = validate_private_path(args.out)
    if out.exists() or Path(str(out) + ".journal.json").exists() or Path(str(out) + ".lock").exists():
        _fail("alarm_plan_exists", "计划或相关执行记录已存在；不能覆盖，应使用新的私有计划文件名。")
    spec = normalize_spec(_read_json(spec_path))
    target = _target(args, session)
    topic = _topic(session, spec["topic_id"])
    client = session.client("monitoring")
    existing = _same_name(client, spec["compartment_id"], spec["display_name"])
    observed = None
    if "alarm_id" in spec:
        if set(existing) - {spec["alarm_id"]}:
            _fail("duplicate_alarm_name", "目标名称已被其他告警使用；不能按名称覆盖或选择其中一个。")
        observed = _get_alarm(client, spec["alarm_id"], target["instance_id"])
        if observed["fields"]["compartment_id"] != spec["compartment_id"]:
            _fail("alarm_compartment_mismatch", "更新 spec 必须明确已有告警所在 compartment；本入口不移动告警。")
        old_name = observed["fields"]["display_name"]
        if old_name != spec["display_name"]:
            previous = _same_name(client, spec["compartment_id"], old_name)
            if set(previous) - {spec["alarm_id"]}:
                _fail("duplicate_alarm_name", "已有告警名称重复；请先按 OCID 核验，不能自动覆盖。")
    elif existing:
        _fail("duplicate_alarm_name", "同 compartment 已有同名 ACTIVE/DELETING 告警；创建计划不会覆盖它。需要更新时显式填写 alarm_id。")
    created = _now()
    plan_id = uuid.uuid4().hex
    plan = {"plan_id": plan_id, "operation": "update" if observed else "create",
            "created_at": _iso(created), "expires_at": _iso(created + PLAN_TTL),
            "target": target, "spec": spec, "desired": _desired(spec, target["instance_id"], plan_id),
            "observed": {"topic": topic, "alarm": observed}, "confirmation_required": NOTICE}
    plan["review"] = _review(plan)
    digest = _digest(plan)
    _write_json(out, {"schema_version": 1, "sha256": digest, "plan": plan}, exclusive=True)
    return {"ok": True, "status": "confirmation_required", "action": "plan-alarm", "plan_path": str(out),
            "plan_sha256": digest, "expires_at": plan["expires_at"], "review": plan["review"],
            "cloud_write_attempted": False, "message": NOTICE}


def _receipt(path, digest, state, alarm_id=None, code=None):
    data = {"schema_version": 1, "plan_sha256": digest, "state": state, "updated_at": _iso(_now()),
            "alarm_id": alarm_id, "code": code}
    _write_json(path, data)
    return data


def _load_receipt(path, digest):
    if not path.exists():
        return None
    data = _read_json(path)
    if (not isinstance(data, dict) or set(data) != {"schema_version", "plan_sha256", "state", "updated_at", "alarm_id", "code"}
            or data["schema_version"] != 1 or data["plan_sha256"] != digest
            or data["state"] not in {"started", "uncertain", "succeeded", "failed"}):
        _fail("invalid_alarm_receipt", "执行记录与计划不一致；保留记录并只读核验，不能删除后重试。")
    if data["alarm_id"] is not None:
        _ocid(data["alarm_id"], ("alarm",), "receipt alarm_id")
    return data


def _find_result(plan, client, receipt=None):
    """A fresh GET verifies every managed field, never just the display name."""
    spec = plan["spec"]
    names = _same_name(client, spec["compartment_id"], spec["display_name"])
    if len(names) > 1:
        _fail("duplicate_alarm_name", "写入后发现同名告警，结果需人工核验；不会自动删除或重建。")
    identifier = (receipt or {}).get("alarm_id") or spec.get("alarm_id")
    if identifier is None and names:
        identifier = next(iter(names))
    if identifier is None:
        return None
    if names and set(names) != {identifier}:
        _fail("duplicate_alarm_name", "同名资源与本次执行记录的 OCID 不一致；不能认定执行成功。")
    return _get_alarm(client, identifier, plan["target"]["instance_id"], missing_ok=True)


def _result(status, digest, alarm_id=None, code=None, message=None, attempted=False):
    return {"ok": status in {"applied", "already_applied", "recovered"}, "status": status,
            "action": "apply-alarm", "plan_sha256": digest, "alarm_id": alarm_id,
            "code": code, "message": message, "cloud_write_attempted": attempted}


def _recover(plan, client, receipt, receipt_path, digest, attempted=False):
    try:
        current = _find_result(plan, client, receipt)
        if _matches(plan, current):
            try:
                _receipt(receipt_path, digest, "succeeded", current["alarm_id"])
            except SkillError:
                return _result("partial_failure", digest, current["alarm_id"], "receipt_write_failed",
                               "云端完整受管字段已核验符合计划，但本地成功记录保存失败。保留计划及记录，后续只读核验。", attempted)
            state = "already_applied" if receipt["state"] == "succeeded" else "recovered"
            return _result(state, digest, current["alarm_id"], message="已通过实时读取核验目标与全部受管字段；未重复提交写入。", attempted=attempted)
    except SkillError:
        pass
    if receipt["state"] == "failed":
        return _result("failed", digest, receipt.get("alarm_id"), receipt.get("code"),
                       "此计划先前写入已返回明确失败，未重试。核验原因及云端状态后，生成并重新确认新计划。", attempted)
    return _result("uncertain", digest, receipt.get("alarm_id"), "alarm_result_uncertain",
                   "已有写入尝试，但当前读取不能证明全部字段符合计划。此计划后续仅只读恢复，不会再次创建或更新；保留 journal，先在 OCI 核验。", attempted)


def apply_alarm(args, session):
    approval = getattr(args, "approve_plan", None)
    if not approval:
        _fail("alarm_approval_required", "默认拒绝告警创建和更新。先向用户展示具体计划并明确询问，确认后才传入 --approve-plan <SHA256>；不读取交互输入。")
    if not isinstance(approval, str) or not re.fullmatch(r"[0-9a-f]{64}", approval):
        _fail("alarm_approval_required", "--approve-plan 必须是已展示并获用户确认的计划 SHA256。")
    path = validate_private_path(args.plan, must_exist=True)
    _load_plan(path, approval)
    with _apply_lock(path, approval):
        plan = _load_plan(path, approval)
        receipt_path = validate_private_path(str(path) + ".journal.json")
        receipt = _load_receipt(receipt_path, approval)
        if _target(args, session) != plan["target"] or _topic(session, plan["spec"]["topic_id"]) != plan["observed"]["topic"]:
            _fail("alarm_plan_stale", "实例绑定、实时目标或 topic 状态已改变；停止执行并重新核验。")
        client = session.client("monitoring")
        if receipt:
            return _recover(plan, client, receipt, receipt_path, approval)
        if plan["operation"] == "create":
            names = _same_name(client, plan["spec"]["compartment_id"], plan["spec"]["display_name"])
            if names:
                # A copied plan or lost local receipt can only reconcile its own tagged alarm.
                current = _find_result(plan, client)
                if _matches(plan, current):
                    receipt = _receipt(receipt_path, approval, "succeeded", current["alarm_id"])
                    return _result("already_applied", approval, current["alarm_id"], message="本计划标记及全部受管字段已存在；未重复创建。")
                _fail("duplicate_alarm_name", "计划后出现同名资源；不创建、不覆盖，需重新核验。")
        else:
            current = _get_alarm(client, plan["spec"]["alarm_id"], plan["target"]["instance_id"])
            if current != plan["observed"]["alarm"]:
                _fail("alarm_plan_stale", "告警当前状态或 etag 已改变；原计划不能继续更新。")
            names = _same_name(client, plan["spec"]["compartment_id"], plan["spec"]["display_name"])
            if set(names) - {plan["spec"]["alarm_id"]}:
                _fail("duplicate_alarm_name", "计划后出现同名告警；停止更新。")
        # Validate SDK models before recording a write attempt. No auth material enters the plan.
        try:
            model_name = "CreateAlarmDetails" if plan["operation"] == "create" else "UpdateAlarmDetails"
            details = getattr(session.sdk.monitoring.models, model_name)(**plan["desired"])
            declared = getattr(details, "swagger_types", {})
            if any(key not in declared or _jsonable(getattr(details, key, None)) != value
                   for key, value in plan["desired"].items()):
                raise ValueError("SDK omitted a requested field")
            no_retry = session.sdk.retry.NoneRetryStrategy()
        except Exception:
            _fail("unsupported_oci_sdk", "当前 OCI SDK 不支持所需告警字段；未提交云端写入，请核对 SDK 版本。")
        # Preflight reads can take time. Recheck expiry and on-disk content at the
        # final write boundary, not only when apply first started.
        _load_plan(path, approval)
        receipt = _receipt(receipt_path, approval, "started", plan["spec"].get("alarm_id"))
        try:
            if plan["operation"] == "create":
                response = client.create_alarm(details, opc_retry_token=plan["plan_id"], retry_strategy=no_retry)
                identifier = _ocid(_get(response.data, "id"), ("alarm",), "创建结果 alarm_id")
            else:
                client.update_alarm(plan["spec"]["alarm_id"], details,
                                    if_match=plan["observed"]["alarm"]["etag"], retry_strategy=no_retry)
                identifier = plan["spec"]["alarm_id"]
            receipt = _receipt(receipt_path, approval, "uncertain", identifier)
        except Exception as exc:
            status = _http_status(exc)
            definite = status in {400, 401, 403, 404, 409, 412, 413, 422, 429}
            state = "failed" if definite else "uncertain"
            code = "alarm_conflict" if status in {409, 412} else ("alarm_write_rejected" if definite else "alarm_result_uncertain")
            try:
                receipt = _receipt(receipt_path, approval, state, receipt.get("alarm_id"), code)
            except SkillError:
                return _result("partial_failure", approval, receipt.get("alarm_id"), "receipt_write_failed",
                               "已尝试云端写入，但结果记录失败；保留 started 记录，后续只能只读核验，不能盲目重建。", True)
            if definite:
                return _result("conflict" if status in {409, 412} else "failed", approval,
                               receipt.get("alarm_id"), code, f"OCI 拒绝本次写入（HTTP {status}）；未重试。", True)
            return _recover(plan, client, receipt, receipt_path, approval, attempted=True)
        result = _recover(plan, client, receipt, receipt_path, approval, attempted=True)
        if result["status"] == "recovered":
            result["status"] = "applied"
            result["message"] = "已提交一次写入，并重新读取核验全部受管字段与目标关联。"
        return result


def add_subparsers(monitoring_subparsers):
    plan = monitoring_subparsers.add_parser("plan-alarm", help="只读核验并保存需用户确认的私有告警计划")
    plan.add_argument("--spec", required=True, help="skill/Git 外的 spec JSON 绝对路径")
    plan.add_argument("--out", required=True, help="新的私有计划 JSON 绝对路径；不覆盖已有计划")
    apply = monitoring_subparsers.add_parser("apply-alarm", help="仅执行已展示并获明确确认的具体告警计划")
    apply.add_argument("--plan", required=True, help="私有计划 JSON 绝对路径")
    apply.add_argument("--approve-plan", help="用户明确确认此具体计划后传入的 SHA256；缺省即拒绝")


def handle(args, session):
    if args.action == "plan-alarm":
        return plan_alarm(args, session)
    if args.action == "apply-alarm":
        return apply_alarm(args, session)
    _fail("unsupported_alarm_action", "告警入口仅支持 plan-alarm 和 apply-alarm；不提供删除操作。")
