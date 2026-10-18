"""Offline risk tests: every OCI response/client/credential file is synthetic."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import oci_ops
from oci_support import OciSession, collect_pages, safe_error
from server_config import SkillError

try:
    import oci as REAL_OCI
except ImportError:
    REAL_OCI = None


INSTANCE = "ocid1.instance.oc1.test.instance"
COMPARTMENT = "ocid1.compartment.oc1..instance"
OTHER = "ocid1.compartment.oc1..network"
VNIC = "ocid1.vnic.oc1.test.vnic"
VOLUME = "ocid1.volume.oc1.test.volume"
BOOT = "ocid1.bootvolume.oc1.test.boot"
SUBNET = "ocid1.subnet.oc1.test.subnet"
SECRET = "SYNTHETIC_CREDENTIAL_MUST_NEVER_APPEAR"


def response(data, next_page=None):
    return Obj(data=data, headers={"opc-next-page": next_page} if next_page else {})


class ServiceFailure(Exception):
    def __init__(self, status):
        super().__init__(SECRET)
        self.status = status


class FakeCompute:
    def __init__(self):
        self.instance = Obj(id=INSTANCE, compartment_id=COMPARTMENT, availability_domain="TEST:AD-1",
                            region="iad",
                            metadata={"user_data": SECRET}, extended_metadata={"secret": SECRET})

    def get_instance(self, instance_id, **kwargs):
        assert instance_id == INSTANCE
        return response(self.instance)

    def list_vnic_attachments(self, compartment_id, **kwargs):
        assert compartment_id == COMPARTMENT and kwargs["instance_id"] == INSTANCE
        return response([Obj(id="attachment", instance_id=INSTANCE, vnic_id=VNIC, lifecycle_state="ATTACHED")])

    def list_volume_attachments(self, compartment_id, **kwargs):
        assert compartment_id == COMPARTMENT and kwargs["instance_id"] == INSTANCE
        return response([Obj(id="volume-attachment", instance_id=INSTANCE, volume_id=VOLUME,
                             lifecycle_state="ATTACHED", iscsi_secret=SECRET)])

    def list_boot_volume_attachments(self, availability_domain, compartment_id, **kwargs):
        assert availability_domain == "TEST:AD-1" and compartment_id == COMPARTMENT
        assert kwargs["instance_id"] == INSTANCE
        return response([Obj(id="boot-attachment", instance_id=INSTANCE, boot_volume_id=BOOT, lifecycle_state="ATTACHED")])


class FakeNetwork:
    def __init__(self):
        self.vnic = Obj(id=VNIC, compartment_id=OTHER, subnet_id=SUBNET, nsg_ids=[], route_table_id="ocid1.routetable.oc1.test.vnic")

    def get_vnic(self, object_id, **kwargs):
        assert object_id == VNIC
        return response(self.vnic)

    def get_subnet(self, object_id, **kwargs):
        assert object_id == SUBNET
        return response(Obj(id=SUBNET, compartment_id=OTHER, vcn_id="vcn", security_list_ids=[],
                            route_table_id="ocid1.routetable.oc1.test.subnet"))

    def list_private_ips(self, **kwargs):
        assert kwargs["vnic_id"] == VNIC
        return response([Obj(id="ocid1.privateip.oc1.test.private", vnic_id=VNIC,
                             route_table_id="ocid1.routetable.oc1.test.private")])

    def list_ipv6s(self, **kwargs):
        assert kwargs["vnic_id"] == VNIC
        return response([Obj(id="ocid1.ipv6.oc1.test.ipv6", vnic_id=VNIC,
                             route_table_id="ocid1.routetable.oc1.test.ipv6")])

    def get_route_table(self, object_id, **kwargs):
        assert object_id != "ocid1.routetable.oc1.test.subnet", "A more specific empty table must not fall back"
        return response(Obj(id=object_id, compartment_id=OTHER, route_rules=[]))


class FakeBlockstorage:
    def get_volume(self, object_id, **kwargs):
        assert object_id == VOLUME
        return response(Obj(id=VOLUME, compartment_id=OTHER, size_in_gbs=50))

    def get_boot_volume(self, object_id, **kwargs):
        assert object_id == BOOT
        return response(Obj(id=BOOT, compartment_id=OTHER, size_in_gbs=47))


class FakeMonitoring:
    def __init__(self):
        self.last_query = None

    def list_metrics(self, compartment_id, details, **kwargs):
        assert details.dimension_filters == {"resourceId": INSTANCE}
        assert not hasattr(details, "group_by") and "resource_id" not in kwargs
        return response([Obj(name="CpuUtilization", namespace="oci_computeagent", dimensions={"resourceId": INSTANCE})])

    def list_alarms_status(self, compartment_id, **kwargs):
        assert kwargs["resource_id"] == INSTANCE
        return response([Obj(id="alarm-associated", status="OK", severity="WARNING",
                             timestamp_triggered="2026-01-01T00:00:00Z",
                             suppression=Obj(time_suppress_from="2026-01-01T00:00:00Z",
                                             time_suppress_until="2026-01-01T01:00:00Z", description=SECRET))])

    def list_alarms(self, compartment_id, **kwargs):
        return response([Obj(id="alarm-unscoped", query="CpuUtilization[1m].mean() > 90", display_name="Other alarm")])

    def get_alarm(self, object_id, **kwargs):
        assert object_id == "alarm-associated"
        return response(Obj(id=object_id, destinations=["topic-associated"], body=SECRET))

    def summarize_metrics_data(self, compartment_id, details, **kwargs):
        self.last_query = details
        return response([Obj(name="CpuUtilization", namespace="oci_computeagent", dimensions={"resourceId": INSTANCE},
                             metadata={"secret": SECRET}, aggregated_datapoints=[Obj(timestamp=datetime.now(timezone.utc), value=7.5)])])


class FakeNotificationControl:
    def list_topics(self, **kwargs):
        return response([Obj(topic_id="topic-associated", lifecycle_state="ACTIVE"), Obj(topic_id="topic-unscoped")])


class FakeNotificationData:
    def list_subscriptions(self, compartment_id, **kwargs):
        return response([Obj(id="subscription", topic_id="topic-associated", protocol="EMAIL", lifecycle_state="PENDING", endpoint=SECRET)])


class FakeSession:
    def __init__(self):
        self.instance_id = INSTANCE
        self.compartment_id = COMPARTMENT
        self.region = "us-ashburn-1"
        self.settings = {"metric_compartment_id": OTHER, "alarm_compartment_id": OTHER,
                         "notification_compartment_id": OTHER}
        self.clients = {"compute": FakeCompute(), "network": FakeNetwork(), "blockstorage": FakeBlockstorage(),
                        "monitoring": FakeMonitoring(), "notification_control": FakeNotificationControl(),
                        "notification_data": FakeNotificationData()}
        self.sdk = Obj(monitoring=Obj(models=Obj(ListMetricsDetails=Obj, SummarizeMetricsDataDetails=Obj)))
        self.ensured = 0

    def client(self, name):
        return self.clients[name]

    def call(self, method, *args, **kwargs):
        return method(*args, **kwargs)

    def ensure_instance(self):
        self.ensured += 1
        return self.clients["compute"].instance


class PaginationTests(unittest.TestCase):
    def test_cap_is_explicit_and_rows_are_preserved(self):
        calls = []
        def listing(**kwargs):
            calls.append(kwargs)
            return response([len(calls)], "page-" + str(len(calls)))
        result = collect_pages(listing, max_pages=2)
        self.assertEqual(result["items"], [1, 2])
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["code"], "pagination_limit")
        self.assertEqual(calls[1]["page"], "page-1")
        self.assertEqual(calls[0]["limit"], 100)

    def test_permission_failure_after_page_keeps_evidence(self):
        def listing(**kwargs):
            if "page" in kwargs:
                raise ServiceFailure(403)
            return response(["first"], "next")
        result = collect_pages(listing)
        self.assertEqual(result["items"], ["first"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["code"], "oci_forbidden")
        self.assertNotIn(SECRET, json.dumps(result))

    def test_repeated_token_stops_and_collection_items_work(self):
        result = collect_pages(lambda **kwargs: response(Obj(items=[1]), "same"))
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["errors"][0]["code"], "pagination_loop")

    def test_true_empty_is_distinct_from_404(self):
        self.assertTrue(collect_pages(lambda **kwargs: response([]))["complete"])
        def missing(**kwargs):
            raise ServiceFailure(404)
        result = collect_pages(missing)
        self.assertFalse(result["complete"])
        self.assertEqual(result["errors"][0]["code"], "oci_not_authorized_or_not_found")


class EvidenceTests(unittest.TestCase):
    def test_inventory_follows_ids_across_compartments_and_omits_secrets(self):
        result = oci_ops.inventory(FakeSession())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["vnics"][0]["vnic"]["compartment_id"], OTHER)
        self.assertEqual(result["data"]["block_volumes"][0]["volume"]["compartment_id"], OTHER)
        self.assertEqual(result["data"]["boot_volumes"][0]["volume"]["id"], BOOT)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn("metadata", result["data"]["instance"])

    def test_attachment_target_mismatch_is_excluded(self):
        session = FakeSession()
        session.clients["compute"].list_vnic_attachments = lambda *args, **kwargs: response([
            Obj(instance_id="another-instance", vnic_id=VNIC)])
        result = oci_ops.inventory(session)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["vnics"], [])
        self.assertEqual(result["errors"][0]["code"], "relationship_mismatch")

    def test_network_uses_single_specific_table_and_does_not_claim_reachability(self):
        result = oci_ops.network(FakeSession())
        self.assertEqual(result["status"], "ok")
        vnic = result["data"]["vnics"][0]
        self.assertEqual(vnic["private_ips"][0]["effective_route"]["selected_from"], "private_ip")
        self.assertEqual(vnic["ipv6s"][0]["effective_route"]["selected_from"], "ipv6")
        tables = {table["id"] for table in result["data"]["route_tables"]}
        self.assertNotIn("ocid1.routetable.oc1.test.subnet", tables)
        self.assertEqual(result["data"]["reachability"], "not_evaluated")

    def test_network_permission_gap_preserves_other_evidence(self):
        session = FakeSession()
        def denied(*args, **kwargs):
            raise ServiceFailure(403)
        session.clients["network"].get_subnet = denied
        result = oci_ops.network(session)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["data"]["vnics"][0]["subnet"])
        self.assertEqual(len(result["data"]["route_tables"]), 3)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_get_id_mismatch_is_not_accepted(self):
        session = FakeSession()
        session.clients["network"].vnic.id = "wrong-vnic"
        result = oci_ops.inventory(session)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["data"]["vnics"][0]["vnic"])

    def test_monitoring_associations_and_subscription_privacy(self):
        result = oci_ops.monitoring_inspect(FakeSession())
        self.assertEqual(result["status"], "ok")
        alarms = {row["id"]: row for row in result["data"]["alarms"]}
        self.assertEqual(alarms["alarm-associated"]["association"], "resource_id_status_filter")
        self.assertEqual(alarms["alarm-unscoped"]["association"], "unscoped_compartment_alarm")
        self.assertIsNone(alarms["alarm-unscoped"]["status"])
        self.assertEqual(alarms["alarm-associated"]["status"]["timestamp_triggered"], "2026-01-01T00:00:00Z")
        self.assertEqual(alarms["alarm-associated"]["status"]["suppression"]["time_suppress_until"], "2026-01-01T01:00:00Z")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(result["data"]["subscriptions"][0]["lifecycle_state"], "PENDING")

    def test_metric_query_is_bound_and_result_dimensions_are_verified(self):
        session = FakeSession()
        monitoring = session.clients["monitoring"]
        result = oci_ops.monitoring_query(session, "oci_computeagent", "CpuUtilization")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(monitoring.last_query.query, f'CpuUtilization[1m]{{resourceId = "{INSTANCE}"}}.mean()')
        self.assertEqual(result["data"]["datapoint_count"], 1)
        self.assertNotIn(SECRET, json.dumps(result))
        monitoring.summarize_metrics_data = lambda *args, **kwargs: response([
            Obj(name="CpuUtilization", namespace="oci_computeagent", dimensions={"resourceId": "wrong"})])
        result = oci_ops.monitoring_query(session, "oci_computeagent", "CpuUtilization")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["streams"], [])

    def test_metric_query_rejects_mql_injection_and_excessive_time_before_read(self):
        session = FakeSession()
        for kwargs in ({"metric": 'Cpu[1m].mean() || secret'}, {"hours": 25}, {"hours": float("nan")}, {"statistic": "delete"}):
            arguments = dict(namespace="oci_computeagent", metric="CpuUtilization", **{})
            arguments.update(kwargs)
            with self.assertRaises(SkillError):
                oci_ops.monitoring_query(session, **arguments)
        self.assertEqual(session.ensured, 0)

    def test_point_truncation_is_partial(self):
        session = FakeSession()
        session.clients["monitoring"].summarize_metrics_data = lambda *args, **kwargs: response([
            Obj(name="CpuUtilization", namespace="oci_computeagent", dimensions={"resourceId": INSTANCE},
                aggregated_datapoints=[Obj(value=1)] * 4)])
        with patch.object(oci_ops, "MAX_QUERY_POINTS", 2):
            result = oci_ops.monitoring_query(session, "oci_computeagent", "CpuUtilization")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["datapoint_count"], 2)

    def test_custom_filesystem_metric_preserves_each_mountpoint(self):
        session = FakeSession()
        streams = [Obj(name="DiskUsedPercent", namespace="custom_filesystem",
                       dimensions={"resourceId": INSTANCE, "mountpoint": mount},
                       aggregated_datapoints=[Obj(value=50)]) for mount in ("/", "/data")]
        session.clients["monitoring"].summarize_metrics_data = lambda *args, **kwargs: response(streams)
        result = oci_ops.monitoring_query(session, "custom_filesystem", "DiskUsedPercent")
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["dimensions"]["mountpoint"] for row in result["data"]["streams"]], ["/", "/data"])


class SessionTests(unittest.TestCase):
    def setUp(self):
        # These tests own all synthetic files. Storage-policy tests live in the
        # configuration suite; managed test roots may themselves contain .git.
        def synthetic_path(raw, must_exist=False, kind="file"):
            path = Path(raw)
            self.assertTrue(path.is_absolute())
            if must_exist:
                self.assertTrue(path.is_file())
            return path
        for module in ("server_config", "oci_support"):
            mocker = patch(module + ".validate_private_path", side_effect=synthetic_path)
            mocker.start()
            self.addCleanup(mocker.stop)

    def sdk_and_profile(self, directory, auth_mode="api_key"):
        config = Path(directory) / "config"
        key = Path(directory) / "synthetic-key"
        token = Path(directory) / "synthetic-token"
        config.write_text("synthetic config", encoding="utf-8")
        key.write_text("synthetic key, not a private key", encoding="utf-8")
        token.write_text("synthetic-token", encoding="utf-8")
        profile = {"oci": {"config_file": str(config), "config_profile": "TEST", "auth_mode": auth_mode,
            "region": "us-ashburn-1", "instance_id": INSTANCE, "compartment_id": COMPARTMENT,
            "confirmed_at": "2026-01-01T00:00:00Z"}}
        captured = {}
        def from_file(**kwargs):
            captured["from_file"] = kwargs
            return {"key_file": str(key), "security_token_file": str(token), "region": "wrong-region",
                    "tenancy": "synthetic-tenancy", "user": "synthetic-user", "fingerprint": "synthetic-fingerprint",
                    "log_requests": True, "additional_secret": SECRET}
        def retry_builder(**kwargs):
            captured["retry"] = kwargs
            return Obj(get_retry_strategy=lambda: "bounded-retry")
        def client(config, **kwargs):
            captured["client_config"] = config
            captured["client_options"] = kwargs
            return FakeCompute()
        def signer(token_value, key_value):
            captured["token_signer"] = (token_value, key_value)
            return "synthetic-signer"
        sdk = Obj(config=Obj(from_file=from_file), retry=Obj(RetryStrategyBuilder=retry_builder),
                  core=Obj(ComputeClient=client), signer=Obj(load_private_key_from_file=lambda *a, **kw: "synthetic-loaded-key"),
                  auth=Obj(signers=Obj(SecurityTokenSigner=signer)))
        return sdk, profile, captured

    def test_named_profile_explicit_region_timeouts_retry_and_client_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk, profile, captured = self.sdk_and_profile(directory)
            with patch("oci_support.importlib.import_module", return_value=sdk):
                session = OciSession(profile)
            self.assertIs(session.client("compute"), session.client("compute"))
            self.assertEqual(captured["from_file"]["profile_name"], "TEST")
            self.assertEqual(captured["client_config"]["region"], "us-ashburn-1")
            self.assertNotIn("additional_secret", captured["client_config"])
            self.assertFalse(captured["client_config"]["log_requests"])
            self.assertEqual(captured["client_options"]["timeout"], (5, 15))
            self.assertEqual(captured["retry"]["max_attempts"], 3)
            self.assertEqual(session.ensure_instance().id, INSTANCE)

    def test_security_token_uses_explicit_signer_without_refresh_or_session_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk, profile, captured = self.sdk_and_profile(directory, "security_token")
            with patch("oci_support.importlib.import_module", return_value=sdk):
                session = OciSession(profile)
            session.client("compute")
            self.assertEqual(captured["token_signer"], ("synthetic-token", "synthetic-loaded-key"))
            self.assertEqual(captured["client_options"]["signer"], "synthetic-signer")

    def test_instance_compartment_or_region_mismatch_is_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk, profile, _ = self.sdk_and_profile(directory)
            with patch("oci_support.importlib.import_module", return_value=sdk):
                session = OciSession(profile)
            instance = session.client("compute").instance
            for field, incorrect in (("id", "wrong"), ("compartment_id", OTHER), ("region", "phx"), ("region", None)):
                original = getattr(instance, field)
                setattr(instance, field, incorrect)
                with self.assertRaises(SkillError) as raised:
                    session.ensure_instance()
                self.assertEqual(raised.exception.code, "target_mismatch")
                setattr(instance, field, original)

    def test_sdk_missing_has_static_message(self):
        with tempfile.TemporaryDirectory() as directory:
            _, profile, _ = self.sdk_and_profile(directory)
            with patch("oci_support.importlib.import_module", side_effect=ImportError(SECRET)):
                with self.assertRaises(SkillError) as raised:
                    OciSession(profile)
            self.assertEqual(raised.exception.code, "oci_sdk_missing")
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertNotIn(directory, str(raised.exception))

    def test_client_initialization_error_does_not_expose_sdk_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            sdk, profile, _ = self.sdk_and_profile(directory)
            def broken_client(*args, **kwargs):
                raise ValueError(SECRET + directory)
            sdk.core.ComputeClient = broken_client
            with patch("oci_support.importlib.import_module", return_value=sdk):
                session = OciSession(profile)
            with self.assertRaises(SkillError) as raised:
                session.client("compute")
            self.assertEqual(raised.exception.code, "oci_client_error")
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertNotIn(directory, str(raised.exception))

    def test_read_call_rejects_mutation_before_invocation(self):
        session = object.__new__(OciSession)
        def terminate_instance(*args, **kwargs):
            self.fail("must never invoke mutation")
        with self.assertRaises(SkillError) as raised:
            session.call(terminate_instance, INSTANCE)
        self.assertEqual(raised.exception.code, "read_only_violation")

    def test_invalid_cli_outputs_safe_json(self):
        output = io.StringIO()
        alarm_stub = Obj(add_subparsers=lambda actions: None)
        with patch.dict(sys.modules, {"oci_alarms": alarm_stub}), contextlib.redirect_stdout(output):
            code = oci_ops.main(["--server", "test", "--" + SECRET])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "error")
        self.assertNotIn(SECRET, output.getvalue())

    def test_alarm_business_outcomes_preserve_receipt_and_map_exit_status(self):
        def add_subparsers(actions):
            parser = actions.add_parser("apply-alarm")
            parser.add_argument("--approve-plan")
        for outcome, ok, status, exit_code in (
            ("confirmation_required", True, "ok", 0), ("applied", True, "ok", 0),
            ("already_applied", True, "ok", 0), ("recovered", True, "ok", 0),
            ("uncertain", False, "partial", 2), ("partial_failure", False, "partial", 2),
            ("failed", False, "error", 1), ("conflict", False, "error", 1),
        ):
            business = {"status": outcome, "ok": ok, "cloud_write_attempted": outcome != "confirmation_required",
                        "plan_sha256": "synthetic-hash", "message": "synthetic result"}
            alarm_stub = Obj(add_subparsers=add_subparsers, handle=lambda *args: business)
            output = io.StringIO()
            with patch.dict(sys.modules, {"oci_alarms": alarm_stub}), patch.object(oci_ops, "read_profile", return_value={}), \
                    patch.object(oci_ops, "OciSession", return_value=FakeSession()), contextlib.redirect_stdout(output):
                code = oci_ops.main(["--server", "test", "monitoring", "apply-alarm", "--approve-plan", "a" * 64])
            result = json.loads(output.getvalue())
            self.assertEqual((result["status"], code), (status, exit_code))
            self.assertEqual(result["data"], business)
            self.assertEqual(result["server"], "test")

    def test_alarm_approval_is_required_before_configuration_or_credentials(self):
        def add_subparsers(actions):
            parser = actions.add_parser("apply-alarm")
            parser.add_argument("--approve-plan")
        for approval in (None, "invalid", "A" * 64):
            args = ["--server", "test", "monitoring", "apply-alarm"]
            if approval is not None:
                args.extend(["--approve-plan", approval])
            output = io.StringIO()
            with patch.dict(sys.modules, {"oci_alarms": Obj(add_subparsers=add_subparsers)}), \
                    patch.object(oci_ops, "read_profile", side_effect=AssertionError("Must not read configuration")), \
                    patch.object(oci_ops, "OciSession", side_effect=AssertionError("Must not load credentials")), \
                    contextlib.redirect_stdout(output):
                code = oci_ops.main(args)
            result = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(result["errors"][0]["code"], "alarm_approval_required")


@unittest.skipUnless(REAL_OCI is not None, "Optional official OCI SDK is not installed; fake tests still run")
class RealSdkContractTests(unittest.TestCase):
    def test_real_models_and_method_arguments_with_transport_replaced(self):
        # A deliberately uninitialized token signer has no key, token or ability
        # to sign. Every transport call is replaced, with a socket guard as well.
        signer = object.__new__(REAL_OCI.auth.signers.SecurityTokenSigner)
        retry = REAL_OCI.retry.NoneRetryStrategy()
        calls = []
        def transport(**kwargs):
            calls.append(kwargs)
            return response([])
        with patch("socket.socket.connect", side_effect=AssertionError("Offline test attempted networking")):
            clients = {}
            for name, (module, class_name) in OciSession.CLIENT_TYPES.items():
                client = getattr(getattr(REAL_OCI, module), class_name)(
                    {"region": "us-ashburn-1", "log_requests": False}, signer=signer,
                    retry_strategy=retry, timeout=(5, 15))
                client.base_client.call_api = transport
                clients[name] = client
            details = REAL_OCI.monitoring.models.ListMetricsDetails(dimension_filters={"resourceId": INSTANCE})
            query = REAL_OCI.monitoring.models.SummarizeMetricsDataDetails(namespace="oci_computeagent",
                query=f'CpuUtilization[1m]{{resourceId = "{INSTANCE}"}}.mean()',
                start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc), resolution="1m")
            paging = {"limit": 100, "page": "synthetic-page", "retry_strategy": retry}
            clients["compute"].list_vnic_attachments(COMPARTMENT, instance_id=INSTANCE, **paging)
            clients["compute"].list_volume_attachments(COMPARTMENT, instance_id=INSTANCE, **paging)
            clients["compute"].list_boot_volume_attachments("TEST:AD-1", COMPARTMENT, instance_id=INSTANCE, **paging)
            clients["network"].list_private_ips(vnic_id=VNIC, **paging)
            clients["network"].list_ipv6s(vnic_id=VNIC, **paging)
            clients["network"].list_network_security_group_security_rules("nsg", **paging)
            for method in ("get_vnic", "get_subnet", "get_route_table", "get_private_ip", "get_network_security_group",
                           "get_security_list", "get_internet_gateway", "get_nat_gateway", "get_service_gateway",
                           "get_local_peering_gateway", "get_drg"):
                getattr(clients["network"], method)("synthetic-object", retry_strategy=retry)
            clients["compute"].get_instance(INSTANCE, retry_strategy=retry)
            clients["blockstorage"].get_volume(VOLUME, retry_strategy=retry)
            clients["blockstorage"].get_boot_volume(BOOT, retry_strategy=retry)
            clients["monitoring"].list_metrics(COMPARTMENT, details, **paging)
            clients["monitoring"].list_alarms_status(COMPARTMENT, resource_id=INSTANCE, **paging)
            clients["monitoring"].list_alarms(COMPARTMENT, **paging)
            clients["monitoring"].summarize_metrics_data(COMPARTMENT, query, retry_strategy=retry)
            clients["notification_control"].list_topics(compartment_id=COMPARTMENT, **paging)
            clients["notification_control"].get_topic("synthetic-topic", retry_strategy=retry)
            clients["notification_data"].list_subscriptions(COMPARTMENT, topic_id="synthetic-topic", **paging)
        self.assertGreaterEqual(len(calls), 20)
        self.assertTrue(all(call["method"] in ("GET", "POST") for call in calls))
        self.assertTrue(all(call["operation_name"] in {"list_metrics", "summarize_metrics_data"}
                            for call in calls if call["method"] == "POST"))
        metric_call = next(call for call in calls if call["operation_name"] == "list_metrics")
        self.assertEqual(metric_call["body"].dimension_filters, {"resourceId": INSTANCE})


if __name__ == "__main__":
    unittest.main()
