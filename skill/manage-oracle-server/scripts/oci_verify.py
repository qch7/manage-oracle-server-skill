"""One read-only check for recent, distinct-minute filesystem metrics.

The caller supplies the already selected OciSession. This module never creates
credentials, clients outside that session, SSH connections, or IMDS requests.
No polling or observation cache can turn previously seen values into new data.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import PurePosixPath
import posixpath
import re

from oci_ops import NAME
from oci_support import safe_error, value
from server_config import SkillError


MAX_RESPONSE_STREAMS = 100
MAX_RESPONSE_POINTS = 2500
MAX_MOUNT_LENGTH = 4096
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z"
)
_INSTANCE = re.compile(r"ocid1\.instance\.[A-Za-z0-9._-]{1,480}\Z")
_COMPARTMENT = re.compile(r"ocid1\.(?:compartment|tenancy)\.[A-Za-z0-9._-]{1,480}\Z")
_UNITS = {
    "DiskUsedPercent": "percent", "InodeUsedPercent": "percent",
    "DiskAvailableGiB": "GiB", "DiskAvailableBytes": "bytes",
}


@dataclass(frozen=True)
class VerificationOptions:
    namespace: str
    metrics: tuple[str, ...]
    mountpoints: tuple[str, ...]
    since: datetime
    minimum_points: int
    max_age_minutes: int
    now: datetime

    @property
    def start_time(self):
        span = self.max_age_minutes + self.minimum_points + 1
        return max(self.since, self.now - timedelta(minutes=span))


def _now():
    return datetime.now(timezone.utc)


def _utc_text(stamp):
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(raw):
    """Parse only explicit, aware timestamps; never assign a missing timezone."""
    try:
        if isinstance(raw, str):
            if len(raw) > 40 or not _TIMESTAMP.fullmatch(raw):
                return None
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if not isinstance(raw, datetime) or raw.tzinfo is None or raw.utcoffset() is None:
            return None
        return raw.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def validate_args(args, now=None):
    """Validate without touching a session, profile, credentials, or filesystem.

    CLI dispatch should also call this before constructing OciSession so invalid
    arguments cannot cause the SDK to read a configuration or private key.
    """
    if getattr(args, "action", None) != "verify":
        raise SkillError("unsupported_verify_action", "磁盘验收入口仅支持 monitoring verify。")
    namespace = getattr(args, "namespace", None)
    metrics = getattr(args, "metrics", None)
    if not isinstance(namespace, str) or not NAME.fullmatch(namespace):
        raise SkillError("invalid_metric_name", "namespace 需要字母开头的字母、数字或下划线，最多 128 字符。")
    if (not isinstance(metrics, (list, tuple)) or not 1 <= len(metrics) <= 4
            or any(not isinstance(metric, str) or not NAME.fullmatch(metric) for metric in metrics)
            or len(set(metrics)) != len(metrics)):
        raise SkillError("invalid_metrics", "metrics 需要 1–4 个唯一名称，遵守与 namespace 相同的 NAME 规则；不接受 MQL。")
    mounts = getattr(args, "mountpoints", None)
    if not isinstance(mounts, (list, tuple)) or not 1 <= len(mounts) <= 16:
        raise SkillError("invalid_mountpoints", "mountpoints 需要 1–16 个唯一、规范化的 Linux 绝对挂载点。")
    for mount in mounts:
        if (not isinstance(mount, str) or not 1 <= len(mount) <= MAX_MOUNT_LENGTH
                or not mount.startswith("/") or mount.startswith("//")
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in mount)
                or posixpath.normpath(mount) != mount
                or str(PurePosixPath(mount)) != mount
                or ".." in PurePosixPath(mount).parts):
            raise SkillError("invalid_mountpoints", "挂载点必须是规范 Linux 绝对路径；不接受路径穿越、控制字符、重复或末尾分隔符。")
    if len(set(mounts)) != len(mounts):
        raise SkillError("invalid_mountpoints", "mountpoints 不能重复。")
    minimum = getattr(args, "minimum_points", 2)
    age = getattr(args, "max_age_minutes", 3)
    if type(minimum) is not int or not 1 <= minimum <= 10:
        raise SkillError("invalid_minimum_points", "minimum-points 必须是 1–10 之间的整数。")
    if type(age) is not int or not 1 <= age <= 60:
        raise SkillError("invalid_max_age", "max-age-minutes 必须是 1–60 之间的整数。")
    current = _timestamp(_now() if now is None else now)
    since_raw = getattr(args, "since", None)
    since = _timestamp(since_raw) if isinstance(since_raw, str) else None
    if current is None:
        raise SkillError("invalid_verification_clock", "验收需要明确的当前 UTC 时间。")
    if since is None or not current - timedelta(hours=24) <= since < current:
        raise SkillError("invalid_since", "since 必须是显式带时区的 ISO 8601 部署基准，早于当前时间且不超过 24 小时。")
    return VerificationOptions(namespace, tuple(metrics), tuple(mounts), since, minimum, age, current)


def _check(metric, mount):
    return {
        "metric": metric, "mountpoint": mount, "verification": "incomplete",
        "evidence_complete": True, "stream_count": 0, "ambiguous": False,
        "fresh_minute_count": 0, "latest_point": None,
        "datapoints_examined": 0, "excluded_datapoints": {}, "reasons": [],
    }


def _reason(check, code, message, incomplete=False):
    if not any(item["code"] == code for item in check["reasons"]):
        check["reasons"].append({"code": code, "message": message})
    if incomplete:
        check["evidence_complete"] = False


def _issue(errors, metric, code, message, mount=None):
    error = {"operation": "summarize_metrics_data", "metric": metric,
             "code": code, "message": message}
    if mount is not None:
        error["mountpoint"] = mount
    if not any(all(existing.get(key) == item for key, item in error.items()) for existing in errors):
        errors.append(error)


def _exclude(check, code):
    counts = check["excluded_datapoints"]
    counts[code] = counts.get(code, 0) + 1


def _accept_point(point, metric, check, minutes, options):
    check["datapoints_examined"] += 1
    stamp = _timestamp(value(point, "timestamp"))
    if stamp is None:
        _exclude(check, "invalid_timestamp")
        return
    if stamp <= options.since:
        _exclude(check, "not_after_since")
        return
    if stamp < options.start_time:
        _exclude(check, "before_query_window")
        return
    if stamp > options.now:
        _exclude(check, "future_timestamp")
        return
    raw = value(point, "value")
    try:
        number = float(raw) if type(raw) in (int, float) else float("nan")
    except (ValueError, OverflowError):
        number = float("nan")
    if not math.isfinite(number):
        _exclude(check, "invalid_or_nonfinite_value")
        return
    if ((_UNITS.get(metric) == "percent" and not 0 <= number <= 100)
            or (metric in {"DiskAvailableGiB", "DiskAvailableBytes"} and number < 0)):
        _exclude(check, "out_of_range_value")
        return
    minute = stamp.replace(second=0, microsecond=0)
    if minute in minutes:
        _exclude(check, "duplicate_minute")
    if minute not in minutes or stamp > minutes[minute][0]:
        # Timestamp and value stay paired. Never replace a point's time with now.
        minutes[minute] = (stamp, number)


def _finish(check, minutes, options):
    if check["ambiguous"]:
        minutes = {}
    check["fresh_minute_count"] = len(minutes)
    if minutes:
        stamp, number = max(minutes.values(), key=lambda pair: pair[0])
        check["latest_point"] = {"timestamp": _utc_text(stamp), "value": number,
                                 "unit": _UNITS.get(check["metric"])}
    if not check["evidence_complete"]:
        check["verification"] = "incomplete"
        return
    if check["stream_count"] == 0:
        _reason(check, "no_matching_stream", "查询完成，但未返回此指标和挂载点的准确指标流。")
    elif not minutes:
        _reason(check, "no_new_datapoints", "未发现部署基准之后、查询窗口内的有效新数据点。")
    elif len(minutes) < options.minimum_points:
        _reason(check, "insufficient_new_minutes", "有效新点所在的不同 UTC 分钟数量尚未达到 minimum-points。")
    if minutes and options.now - stamp > timedelta(minutes=options.max_age_minutes):
        _reason(check, "latest_point_too_old", "最近的有效新点已超过 max-age-minutes，旧数据不能证明采集仍在持续。")
    check["verification"] = "pending" if check["reasons"] else "verified"


def _scan_response(response, metric, checks, options, instance_id, compartment):
    """Inspect bounded raw models before projecting evidence into output fields."""
    coverage = {"operation": "summarize_metrics_data", "metric": metric,
                "request_attempted": True, "complete": True,
                "streams_returned": 0, "streams_examined": 0,
                "skipped_unselected_streams": 0, "datapoints_examined": 0}
    errors = []
    by_mount = {check["mountpoint"]: check for check in checks}
    points_by_mount = {mount: {} for mount in options.mountpoints}

    def incomplete(code, message, check=None):
        coverage["complete"] = False
        selected = checks if check is None else [check]
        for target in selected:
            _reason(target, code, message, incomplete=True)
        _issue(errors, metric, code, message, None if check is None else check["mountpoint"])

    rows = value(response, "data")
    if not isinstance(rows, (list, tuple)):
        incomplete("unexpected_response", "OCI 指标响应未提供可完整核验的指标流列表。")
        rows = []
    coverage["streams_returned"] = len(rows)
    headers = value(response, "headers", {}) or {}
    if not isinstance(headers, Mapping):
        incomplete("unexpected_response_headers", "响应头结构异常，无法确认读取证据完整。")
    elif any(isinstance(key, str) and key.lower() == "opc-next-page" and token
             for key, token in headers.items()):
        incomplete("unexpected_continuation", "指标查询返回后续页标记；本次只读单次验收不跟随分页，证据不完整。")
    if len(rows) > MAX_RESPONSE_STREAMS:
        incomplete("stream_limit", "指标流超过本次处理上限；截断证据不能用于宣称验收通过。")
    for stream in rows[:MAX_RESPONSE_STREAMS]:
        coverage["streams_examined"] += 1
        dimensions = value(stream, "dimensions")
        mount = dimensions.get("mountpoint") if isinstance(dimensions, dict) else None
        if not isinstance(mount, str):
            incomplete("invalid_stream_dimensions", "返回指标流缺少可识别的挂载点维度，无法完整核验其归属。")
            continue
        if mount not in by_mount:
            coverage["skipped_unselected_streams"] += 1
            continue
        check = by_mount[mount]
        if (value(stream, "namespace") != options.namespace or value(stream, "name") != metric
                or value(stream, "compartment_id") != compartment
                or dimensions.get("resourceId") != instance_id):
            incomplete("relationship_mismatch", "所选挂载点的原始指标流与指定 namespace、metric、compartment 或实例不匹配，已排除。", check)
            continue
        check["stream_count"] += 1
        if len(dimensions) != 2 or set(dimensions) != {"resourceId", "mountpoint"}:
            check["ambiguous"] = True
            incomplete("ambiguous_dimensions", "所选挂载点包含额外维度，不能将其合并为已确认的采集流。", check)
        if value(stream, "resource_group") not in (None, ""):
            check["ambiguous"] = True
            incomplete("ambiguous_resource_group", "所选挂载点带有 resource_group，无法唯一确认所需采集流。", check)
        if check["stream_count"] > 1:
            check["ambiguous"] = True
            incomplete("ambiguous_streams", "同一 metric 和挂载点返回多个流，不能拼接分钟数据来宣称通过。", check)
        if check["ambiguous"]:
            continue
        points = value(stream, "aggregated_datapoints")
        if not isinstance(points, (list, tuple)):
            incomplete("unexpected_datapoints", "指标流缺少可完整核验的数据点列表。", check)
            continue
        remaining = max(0, MAX_RESPONSE_POINTS - coverage["datapoints_examined"])
        if len(points) > remaining:
            incomplete("datapoint_limit", "数据点超过本次处理上限；受影响指标的截断结果不能用于宣称通过。")
        for point in points[:remaining]:
            _accept_point(point, metric, check, points_by_mount[mount], options)
            coverage["datapoints_examined"] += 1
    for check in checks:
        _finish(check, points_by_mount[check["mountpoint"]], options)
    return coverage, errors


def _result(data, errors, coverage, fatal=False):
    states = [check["verification"] for check in data["checks"]]
    counts = {state: states.count(state) for state in ("verified", "pending", "incomplete")}
    data["check_counts"] = dict(total=len(states), **counts)
    if not errors and all(item["complete"] for item in coverage) and all(state == "verified" for state in states):
        status, verification = "ok", "verified"
    elif errors or counts["incomplete"] or not all(item["complete"] for item in coverage):
        status, verification = ("error" if fatal else "partial"), "incomplete"
    else:
        status, verification = "partial", "pending"
    data["verification"] = verification
    return {"status": status, "data": data, "errors": errors, "coverage": coverage}


def handle(args, session):
    options = validate_args(args)
    checks = [_check(metric, mount) for metric in options.metrics for mount in options.mountpoints]
    data = {
        "verification": "incomplete", "namespace": options.namespace,
        "metrics": list(options.metrics), "mountpoints": list(options.mountpoints),
        "since": _utc_text(options.since), "start_time": _utc_text(options.start_time),
        "end_time": _utc_text(options.now), "minimum_points": options.minimum_points,
        "max_age_minutes": options.max_age_minutes, "resolution": "1m", "statistic": "mean",
        "checks": checks,
    }
    errors, coverage = [], []
    try:
        session.ensure_instance()
        instance_id, region = session.instance_id, session.region
        compartment = session.settings.get("metric_compartment_id", session.compartment_id)
        if (not isinstance(instance_id, str) or not _INSTANCE.fullmatch(instance_id)
                or not isinstance(compartment, str) or not _COMPARTMENT.fullmatch(compartment)):
            raise SkillError("invalid_bound_target", "已绑定实例或指标 compartment 标识无效，不能构造验收查询。")
        data.update(instance_id=instance_id, region=region, metric_compartment_id=compartment)
        coverage.append({"operation": "ensure_instance", "complete": True})
    except Exception as exc:
        errors.append(dict(safe_error(exc), operation="ensure_instance"))
        coverage.append({"operation": "ensure_instance", "complete": False})
        for check in checks:
            _reason(check, "target_unverified", "目标实例未通过严格核验，未查询指标。", incomplete=True)
        return _result(data, errors, coverage, fatal=True)
    responses = 0
    for metric in options.metrics:
        selected = [check for check in checks if check["metric"] == metric]
        attempted = False
        try:
            query = f'{metric}[1m]{{resourceId = {json.dumps(instance_id)}}}.mean()'
            details = session.sdk.monitoring.models.SummarizeMetricsDataDetails(
                namespace=options.namespace, query=query, start_time=options.start_time,
                end_time=options.now, resolution="1m",
            )
            client = session.client("monitoring")
            attempted = True
            response = session.call(client.summarize_metrics_data, compartment, details,
                                    compartment_id_in_subtree=False)
            responses += 1
            observed, issues = _scan_response(response, metric, selected, options, instance_id, compartment)
            observed["query"] = query
            coverage.append(observed)
            errors.extend(issues)
        except Exception as exc:
            error = dict(safe_error(exc), operation="summarize_metrics_data", metric=metric)
            errors.append(error)
            coverage.append({"operation": "summarize_metrics_data", "metric": metric,
                             "request_attempted": attempted, "complete": False})
            for check in selected:
                _reason(check, error["code"], "此指标读取未完成；权限或服务错误不能解释为没有数据。", incomplete=True)
                check["verification"] = "incomplete"
    return _result(data, errors, coverage, fatal=responses == 0)


def add_subparsers(monitoring_subparsers):
    verify = monitoring_subparsers.add_parser("verify", help="只读验收部署之后的持续新磁盘指标；每指标查询一次")
    verify.add_argument("--namespace", required=True)
    verify.add_argument("--metrics", nargs="+", required=True)
    verify.add_argument("--mountpoints", nargs="+", required=True)
    verify.add_argument("--since", required=True, help="带时区的 ISO 8601 部署基准，过去 24 小时内")
    verify.add_argument("--minimum-points", type=int, default=2)
    verify.add_argument("--max-age-minutes", type=int, default=3)
