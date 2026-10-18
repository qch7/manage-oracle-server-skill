#!/usr/bin/env python3
"""Target-bound, read-only OCI inventory, network and Monitoring evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import re
import sys

from server_config import ALIAS, SkillError, read_profile
from oci_support import OciSession, collect_pages, pick, safe_error, scalar, utc_now, value


INSTANCE_FIELDS = (
    "id", "display_name", "compartment_id", "availability_domain", "fault_domain",
    "shape", "region", "lifecycle_state", "time_created",
)
VNIC_FIELDS = (
    "id", "display_name", "compartment_id", "availability_domain", "subnet_id",
    "vlan_id", "private_ip", "public_ip", "is_primary", "lifecycle_state",
    "nsg_ids", "route_table_id", "skip_source_dest_check",
)
IP_FIELDS = ("id", "vnic_id", "subnet_id", "ip_address", "is_primary", "route_table_id")
ATTACHMENT_FIELDS = (
    "id", "instance_id", "compartment_id", "availability_domain", "lifecycle_state",
    "attachment_type", "vnic_id", "volume_id", "boot_volume_id", "is_read_only",
    "is_shareable", "device", "is_pv_encryption_in_transit_enabled",
)
VOLUME_FIELDS = (
    "id", "display_name", "compartment_id", "availability_domain", "lifecycle_state",
    "size_in_gbs", "vpus_per_gb", "is_hydrated", "time_created",
)
ALARM_FIELDS = (
    "id", "display_name", "compartment_id", "namespace", "query",
    "metric_compartment_id", "metric_compartment_id_in_subtree", "is_enabled",
    "severity", "lifecycle_state", "pending_duration", "resolution", "destinations",
    "is_notifications_per_metric_dimension_enabled", "time_updated",
)
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")
MAX_QUERY_STREAMS = 50
MAX_QUERY_POINTS = 2500


class Evidence:
    """Keep failures beside successful evidence, including partial page results."""

    def __init__(self, session):
        self.session = session
        self.errors = []
        self.coverage = []
        self.cache = {}

    def issue(self, operation, code, message):
        self.errors.append({"operation": operation, "code": code, "message": message})

    def list(self, service, method, *args, **kwargs):
        try:
            function = getattr(self.session.client(service), method)
            result = collect_pages(lambda *a, **kw: self.session.call(function, *a, **kw), *args, **kwargs)
        except Exception as exc:
            result = {"items": [], "complete": False, "pages": 0, "errors": [safe_error(exc)]}
        self.errors.extend(dict(error, operation=method) for error in result["errors"])
        self.coverage.append({"operation": method, "complete": result["complete"],
                              "pages": result["pages"], "items": len(result["items"])})
        return result["items"]

    def get(self, service, method, object_id, id_field="id"):
        if not object_id:
            self.issue(method, "missing_relationship", "关联对象缺少标识，无法读取。")
            return None
        cache_key = (service, method, object_id)
        if cache_key not in self.cache:
            try:
                function = getattr(self.session.client(service), method)
                obj = self.session.call(function, object_id).data
                if value(obj, id_field) != object_id:
                    raise SkillError("relationship_mismatch", "OCI 返回的关联对象标识不匹配，未使用该对象。")
                self.cache[cache_key] = obj
            except Exception as exc:
                self.errors.append(dict(safe_error(exc), operation=method))
                self.cache[cache_key] = None
        return self.cache[cache_key]

    def result(self, data):
        return {"status": "partial" if self.errors else "ok", "data": data,
                "coverage": self.coverage, "errors": self.errors}


def instance_record(instance):
    record = pick(instance, *INSTANCE_FIELDS)
    record["shape_config"] = pick(value(instance, "shape_config"),
                                  "ocpus", "memory_in_gbs", "networking_bandwidth_in_gbps")
    # metadata, extended_metadata, user_data and agent plugin configuration are deliberately absent.
    return record


def attachments(evidence, method, *args):
    rows = evidence.list("compute", method, *args, instance_id=evidence.session.instance_id)
    bound = []
    for row in rows:
        if value(row, "instance_id") != evidence.session.instance_id:
            evidence.issue(method, "relationship_mismatch", "附件未指向指定实例，已排除。")
        elif value(row, "lifecycle_state") != "DETACHED":
            bound.append(row)
    return bound


def attached_vnics(evidence):
    return [(row, evidence.get("network", "get_vnic", value(row, "vnic_id")))
            for row in attachments(evidence, "list_vnic_attachments", evidence.session.compartment_id)]


def inventory(session):
    instance = session.ensure_instance()
    evidence = Evidence(session)
    data = {"region": session.region, "instance": instance_record(instance), "vnics": [],
            "boot_volumes": [], "block_volumes": []}
    for attachment, vnic in attached_vnics(evidence):
        data["vnics"].append({"attachment": pick(attachment, *ATTACHMENT_FIELDS),
                              "vnic": pick(vnic, *VNIC_FIELDS) if vnic is not None else None})
    block = attachments(evidence, "list_volume_attachments", session.compartment_id)
    availability_domain = value(instance, "availability_domain")
    if availability_domain:
        boot = attachments(evidence, "list_boot_volume_attachments", availability_domain, session.compartment_id)
    else:
        boot = []
        evidence.issue("list_boot_volume_attachments", "missing_availability_domain", "实例未返回 AD，无法确认启动卷附件。")
    for rows, field, method, destination in (
        (block, "volume_id", "get_volume", "block_volumes"),
        (boot, "boot_volume_id", "get_boot_volume", "boot_volumes"),
    ):
        for attachment in rows:
            volume = evidence.get("blockstorage", method, value(attachment, field))
            data[destination].append({"attachment": pick(attachment, *ATTACHMENT_FIELDS),
                                      "volume": pick(volume, *VOLUME_FIELDS) if volume is not None else None})
    data["scope"] = "通过指定实例的真实附件读取关联对象；关联资源允许位于不同 compartment。"
    return evidence.result(data)


def security_rule(rule):
    record = pick(rule, "id", "direction", "protocol", "is_stateless", "is_valid",
                  "source", "source_type", "destination", "destination_type")
    for protocol in ("tcp_options", "udp_options"):
        options = value(rule, protocol)
        if options is not None:
            record[protocol] = {field: pick(value(options, field), "min", "max")
                                for field in ("source_port_range", "destination_port_range")
                                if value(options, field) is not None}
    if value(rule, "icmp_options") is not None:
        record["icmp_options"] = pick(value(rule, "icmp_options"), "type", "code")
    return record


def effective_route(private_ip, vnic, subnet, address_kind="private_ip"):
    for source, obj in ((address_kind, private_ip), ("vnic", vnic), ("subnet", subnet)):
        route_table_id = value(obj, "route_table_id")
        if route_table_id:
            return {"route_table_id": route_table_id, "selected_from": source}
    return {"route_table_id": None, "selected_from": "unknown"}


def network(session):
    instance = session.ensure_instance()
    evidence = Evidence(session)
    data = {"region": session.region, "instance": instance_record(instance), "vnics": [],
            "route_tables": [], "route_targets": [], "reachability": "not_evaluated"}
    route_ids = set()
    for attachment, vnic in attached_vnics(evidence):
        record = {"attachment": pick(attachment, *ATTACHMENT_FIELDS), "vnic": None}
        data["vnics"].append(record)
        if vnic is None:
            continue
        record["vnic"] = pick(vnic, *VNIC_FIELDS)
        subnet = evidence.get("network", "get_subnet", value(vnic, "subnet_id"))
        record["subnet"] = pick(subnet, "id", "compartment_id", "vcn_id", "cidr_block",
                                "ipv6cidr_blocks", "route_table_id", "security_list_ids",
                                "prohibit_public_ip_on_vnic", "prohibit_internet_ingress") if subnet is not None else None
        record["vnic_default_route"] = effective_route(None, vnic, subnet)
        for field, method, address_kind in (("private_ips", "list_private_ips", "private_ip"),
                                             ("ipv6s", "list_ipv6s", "ipv6")):
            record[field] = []
            for private_ip in evidence.list("network", method, vnic_id=value(vnic, "id")):
                if value(private_ip, "vnic_id") != value(vnic, "id"):
                    evidence.issue(method, "relationship_mismatch", "IP 地址未指向已核实的 VNIC，已排除。")
                    continue
                item = pick(private_ip, *IP_FIELDS)
                item["effective_route"] = effective_route(private_ip, vnic, subnet, address_kind)
                record[field].append(item)
        bindings = [record["vnic_default_route"]] + [ip["effective_route"]
                    for ip in record["private_ips"] + record["ipv6s"]]
        for binding in bindings:
            if binding["route_table_id"]:
                route_ids.add(binding["route_table_id"])
            else:
                evidence.issue("effective_route", "unknown_route_table", "无法确定关联的路由表，不能据此判断网络可达性。")
        record["network_security_groups"] = []
        for nsg_id in value(vnic, "nsg_ids", []) or []:
            nsg = evidence.get("network", "get_network_security_group", nsg_id)
            rules = evidence.list("network", "list_network_security_group_security_rules", nsg_id)
            record["network_security_groups"].append({"id": nsg_id,
                "group": pick(nsg, "id", "compartment_id", "vcn_id", "display_name", "lifecycle_state") if nsg is not None else None,
                "rules": [security_rule(rule) for rule in rules]})
        record["security_lists"] = []
        for sl_id in value(subnet, "security_list_ids", []) or []:
            security_list = evidence.get("network", "get_security_list", sl_id)
            if security_list is None:
                record["security_lists"].append({"id": sl_id, "details": None})
                continue
            item = pick(security_list, "id", "compartment_id", "vcn_id", "display_name", "lifecycle_state")
            item["ingress_security_rules"] = [security_rule(rule) for rule in value(security_list, "ingress_security_rules", []) or []]
            item["egress_security_rules"] = [security_rule(rule) for rule in value(security_list, "egress_security_rules", []) or []]
            record["security_lists"].append(item)
    target_ids = set()
    for route_id in sorted(route_ids):
        route_table = evidence.get("network", "get_route_table", route_id)
        record = {"id": route_id, "details": None}
        if route_table is not None:
            record["details"] = pick(route_table, "id", "compartment_id", "vcn_id", "display_name", "lifecycle_state")
            record["route_rules"] = [pick(rule, "destination", "destination_type", "cidr_block",
                "network_entity_id", "route_type") for rule in value(route_table, "route_rules", []) or []]
            target_ids.update(rule["network_entity_id"] for rule in record["route_rules"] if rule["network_entity_id"])
        data["route_tables"].append(record)
    target_methods = {"internetgateway": "get_internet_gateway", "natgateway": "get_nat_gateway",
        "servicegateway": "get_service_gateway", "localpeeringgateway": "get_local_peering_gateway",
        "drg": "get_drg", "privateip": "get_private_ip"}
    for target_id in sorted(target_ids):
        resource_type = target_id.split(".")[1] if target_id.startswith("ocid1.") else "unknown"
        method = target_methods.get(resource_type)
        target = evidence.get("network", method, target_id) if method else None
        if method is None:
            evidence.issue("route_target", "unsupported_route_target", "路由下一跳类型未包含在本工具的只读白名单中。")
        record = {"id": target_id, "type": resource_type, "details": None}
        if target is not None:
            record["details"] = pick(target, "id", "compartment_id", "vcn_id", "display_name",
                "lifecycle_state", "is_enabled", "block_traffic", "route_table_id", "nat_ip",
                "peering_status", "peer_id", "is_cross_tenancy_peering", "vnic_id", "ip_address")
            if resource_type == "servicegateway":
                record["services"] = [pick(service, "service_id", "service_name")
                                      for service in value(target, "services", []) or []]
        data["route_targets"].append(record)
    data["interpretation"] = {
        "routing": "源 IPv4/IPv6 地址的专用路由表优先，其次 VNIC，再次子网；仅使用选中表，不合并或因缺少目的路由而回退。",
        "security_rules": "适用的 NSG 与 Security List 允许规则共同生效；无需两者分别同时允许。状态跟踪及无状态规则仍需结合具体流量判断。",
        "scope": "证据覆盖当前关联对象和路由表的直接下一跳；未探测端口，未评估来回完整路径、DRG 后续路由、来宾防火墙或进程监听。",
    }
    return evidence.result(data)


def metric_record(metric):
    record = pick(metric, "name", "namespace", "compartment_id", "resource_group")
    dimensions = value(metric, "dimensions", {}) or {}
    record["dimensions"] = {name: scalar(dimensions[name]) for name in (
        "resourceId", "availabilityDomain", "region", "device", "mountPoint", "mountpoint"
    ) if name in dimensions}
    return record


def monitoring_inspect(session, namespace=None):
    session.ensure_instance()
    if namespace is not None and not NAME.fullmatch(namespace):
        raise SkillError("invalid_metric_namespace", "指标 namespace 仅支持字母开头的字母、数字和下划线，最多 128 字符。")
    evidence = Evidence(session)
    metric_compartment = session.settings.get("metric_compartment_id", session.compartment_id)
    alarm_compartment = session.settings.get("alarm_compartment_id", session.compartment_id)
    notification_compartment = session.settings.get("notification_compartment_id", session.compartment_id)
    options = {"dimension_filters": {"resourceId": session.instance_id}}
    if namespace is not None:
        options["namespace"] = namespace
    details = session.sdk.monitoring.models.ListMetricsDetails(**options)
    metrics = evidence.list("monitoring", "list_metrics", metric_compartment, details)
    data = {"instance_id": session.instance_id, "region": session.region,
        "scopes": {"metrics": metric_compartment, "alarms": alarm_compartment,
                   "notifications": notification_compartment, "compartment_subtree": False},
        "metrics": [], "alarms": [], "topics": [], "subscriptions": []}
    for metric in metrics:
        if (value(metric, "dimensions", {}) or {}).get("resourceId") != session.instance_id:
            evidence.issue("list_metrics", "relationship_mismatch", "指标维度未精确匹配实例，已排除。")
        else:
            data["metrics"].append(metric_record(metric))
    statuses = evidence.list("monitoring", "list_alarms_status", alarm_compartment, resource_id=session.instance_id)
    status_by_id = {value(row, "id"): row for row in statuses if value(row, "id")}
    summaries = evidence.list("monitoring", "list_alarms", alarm_compartment)
    alarms = {value(row, "id"): row for row in summaries if value(row, "id")}
    for alarm_id in status_by_id:
        alarms[alarm_id] = evidence.get("monitoring", "get_alarm", alarm_id) or alarms.get(alarm_id)
    associated_topics = set()
    for alarm_id, alarm in alarms.items():
        associated = alarm_id in status_by_id
        record = pick(alarm, *ALARM_FIELDS) if alarm is not None else {"id": alarm_id}
        record["association"] = "resource_id_status_filter" if associated else "unscoped_compartment_alarm"
        record["status"] = pick(status_by_id[alarm_id], "status", "severity", "timestamp_triggered") if associated else None
        if associated:
            suppression = value(status_by_id[alarm_id], "suppression")
            record["status"]["suppression"] = pick(suppression, "time_suppress_from", "time_suppress_until") if suppression is not None else None
        data["alarms"].append(record)
        if associated:
            associated_topics.update(value(alarm, "destinations", []) or [])
    selected_topic = session.settings.get("topic_id")
    if selected_topic:
        topic = evidence.get("notification_control", "get_topic", selected_topic, id_field="topic_id")
        topics = [topic] if topic is not None else []
    else:
        topics = evidence.list("notification_control", "list_topics", compartment_id=notification_compartment)
    data["scopes"]["topic_id"] = selected_topic
    for topic in topics:
        record = pick(topic, "topic_id", "name", "compartment_id", "lifecycle_state", "time_created")
        record["association"] = "referenced_by_target_alarm" if value(topic, "topic_id") in associated_topics else "unscoped_compartment_topic"
        data["topics"].append(record)
    subscription_filter = {"topic_id": selected_topic} if selected_topic else {}
    for subscription in evidence.list("notification_data", "list_subscriptions", notification_compartment, **subscription_filter):
        # Do not print email addresses, webhook URLs, confirmation tokens or endpoints.
        record = pick(subscription, "id", "topic_id", "compartment_id", "protocol", "lifecycle_state", "created_time")
        record["association"] = "referenced_by_target_alarm" if value(subscription, "topic_id") in associated_topics else "unscoped_compartment_subscription"
        data["subscriptions"].append(record)
    data["interpretation"] = {
        "metrics": "仅查询包含准确 resourceId 维度的指标；可见定义为空不等同于实例故障或未安装 agent。",
        "alarms": "resource_id_status_filter 表示 OCI 状态接口按实例筛选，状态仍是整个告警的汇总；unscoped 项仅属于配置 compartment，不能认定监控此实例。",
        "notifications": "配置 topic_id 时直接读取该主题；订阅限制在配置 notifications compartment，并按 topic_id 筛选。其他 compartment 未扫描，ACTIVE 状态不代表已验证送达。",
    }
    return evidence.result(data)


def monitoring_query(session, namespace, metric, hours=1, statistic="mean", interval="1m"):
    if not isinstance(namespace, str) or not NAME.fullmatch(namespace) or not isinstance(metric, str) or not NAME.fullmatch(metric):
        raise SkillError("invalid_metric_name", "namespace 和 metric 仅支持字母开头的字母、数字和下划线，最多 128 字符。")
    if not isinstance(hours, (int, float)) or isinstance(hours, bool) or not math.isfinite(hours) or not 1 / 60 <= hours <= 24:
        raise SkillError("invalid_time_window", "hours 必须在 1/60 至 24 之间。")
    if statistic not in {"mean", "min", "max", "sum", "count"} or interval not in {"1m", "5m", "1h"}:
        raise SkillError("invalid_metric_aggregation", "聚合统计或时间间隔不在允许列表中。")
    session.ensure_instance()
    if not re.fullmatch(r"ocid1\.instance\.[A-Za-z0-9._-]{1,480}", session.instance_id):
        raise SkillError("invalid_instance_id", "实例标识不适合构造受限指标查询。")
    evidence = Evidence(session)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    query = f'{metric}[{interval}]{{resourceId = "{session.instance_id}"}}.{statistic}()'
    compartment = session.settings.get("metric_compartment_id", session.compartment_id)
    details = session.sdk.monitoring.models.SummarizeMetricsDataDetails(
        namespace=namespace, query=query, start_time=start, end_time=end, resolution=interval)
    response = session.call(session.client("monitoring").summarize_metrics_data, compartment, details)
    rows = response.data
    if not isinstance(rows, (list, tuple)):
        raise SkillError("unexpected_response", "OCI 指标响应结构不符合预期。")
    data = {"instance_id": session.instance_id, "region": session.region, "compartment_id": compartment,
        "namespace": namespace, "metric": metric, "query": query, "start_time": start.isoformat(),
        "end_time": end.isoformat(), "streams": [], "datapoint_count": 0}
    if len(rows) > MAX_QUERY_STREAMS:
        evidence.issue("summarize_metrics_data", "result_limit", "指标流超过输出上限，仅保留部分结果。")
    for stream in rows[:MAX_QUERY_STREAMS]:
        if (value(stream, "dimensions", {}) or {}).get("resourceId") != session.instance_id or value(stream, "name") != metric or value(stream, "namespace") != namespace:
            evidence.issue("summarize_metrics_data", "relationship_mismatch", "指标响应未精确匹配实例、名称或 namespace，已排除。")
            continue
        record = metric_record(stream)
        points = value(stream, "aggregated_datapoints", []) or []
        remaining = MAX_QUERY_POINTS - data["datapoint_count"]
        if len(points) > remaining:
            evidence.issue("summarize_metrics_data", "result_limit", "数据点超过输出上限，仅保留部分结果。")
        record["aggregated_datapoints"] = [pick(point, "timestamp", "value") for point in points[:remaining]]
        data["datapoint_count"] += len(record["aggregated_datapoints"])
        data["streams"].append(record)
    data["result"] = "data" if data["datapoint_count"] else "no_visible_datapoints"
    return evidence.result(data)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise SkillError("invalid_arguments", "命令参数无效；请使用 --help 查看受支持的固定命令。")


def build_parser():
    parser = JsonArgumentParser(description="按已确认实例配置读取 OCI 证据；不自动安装依赖或修复资源。")
    parser.add_argument("--server", required=True, help="已配置实例别名")
    parser.add_argument("--config", help="包外 profiles.json 的本机绝对路径")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inventory", help="读取指定实例及实际 VNIC/卷附件")
    commands.add_parser("network", help="读取网络对象与有效路由选择证据")
    monitoring = commands.add_parser("monitoring", help="检查 Monitoring 或查询受限 MQL")
    actions = monitoring.add_subparsers(dest="action", required=True)
    inspect = actions.add_parser("inspect", help="实例指标、告警与配置范围的通知状态")
    inspect.add_argument("--namespace")
    query = actions.add_parser("query", help="按 resourceId 锁定实例查询指标")
    query.add_argument("--namespace", required=True)
    query.add_argument("--metric", required=True)
    query.add_argument("--hours", type=float, default=1)
    query.add_argument("--statistic", choices=("mean", "min", "max", "sum", "count"), default="mean")
    query.add_argument("--interval", choices=("1m", "5m", "1h"), default="1m")
    import oci_alarms
    oci_alarms.add_subparsers(actions)
    import oci_verify
    oci_verify.add_subparsers(actions)
    return parser


def main(argv=None):
    server = None
    try:
        args = build_parser().parse_args(argv)
        server = args.server if ALIAS.fullmatch(args.server) else None
        if (args.command == "monitoring" and args.action == "apply-alarm"
                and not re.fullmatch(r"[0-9a-f]{64}", getattr(args, "approve_plan", None) or "")):
            raise SkillError("alarm_approval_required", "先展示具体告警计划并询问用户；只有明确确认该计划后才能提供准确的 --approve-plan SHA256。")
        if args.command == "monitoring" and args.action == "verify":
            import oci_verify
            oci_verify.validate_args(args)
        session = OciSession(read_profile(args.server, args.config, need_oci=True))
        if args.command == "inventory":
            result = inventory(session)
        elif args.command == "network":
            result = network(session)
        elif args.action == "inspect":
            result = monitoring_inspect(session, args.namespace)
        elif args.action == "query":
            result = monitoring_query(session, args.namespace, args.metric, args.hours, args.statistic, args.interval)
        elif args.action == "verify":
            result = oci_verify.handle(args, session)
        else:
            import oci_alarms
            business = oci_alarms.handle(args, session)
            outcome = business.get("status")
            if outcome in {"uncertain", "partial_failure"}:
                status = "partial"
            elif outcome in {"confirmation_required", "applied", "already_applied", "recovered"} and business.get("ok") is True:
                status = "ok"
            else:
                status = "error"
            result = {"status": status, "data": business, "errors": []}
            if status != "ok":
                result["errors"].append({"code": business.get("code") or "alarm_operation_incomplete",
                                         "message": business.get("message") or "告警操作未完成；请查看保留的完整执行结果。"})
        status = result.get("status", "ok")
        if status not in ("ok", "partial", "error"):
            raise SkillError("invalid_handler_result", "命令返回了无法识别的状态。")
        if "data" not in result:
            result = {"status": status, "data": {k: v for k, v in result.items() if k != "status"}, "errors": []}
        result.update({"status": status, "server": server, "observed_at": utc_now()})
        result.setdefault("errors", [])
    except Exception as exc:
        result = {"status": "error", "server": server, "observed_at": utc_now(),
                  "data": None, "errors": [safe_error(exc)]}
    try:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        result = {"status": "error", "server": server, "observed_at": utc_now(), "data": None,
                  "errors": [{"code": "invalid_output", "message": "无法安全编码结果，未输出原始对象。"}]}
        print(json.dumps(result, ensure_ascii=False))
    return {"ok": 0, "partial": 2, "error": 1}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
