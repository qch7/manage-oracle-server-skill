"""Filesystem and publication risk tests. No real OCI, SSH or IMDS access."""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
import io
import json
import ntpath
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import disk_metrics as dm


def synthetic_config():
    return {"version": 1, "instance_id": "ocid1.instance.oc1.iad.synthetic", "compartment_id": "ocid1.compartment.oc1..synthetic",
            "region": "us-ashburn-1", "namespace": "personal_server", "mountpoints": ["/"]}


def approved_config():
    config = synthetic_config()
    config["publishing_approval"] = {"scope_sha256": dm.scope_hash(config), "confirmed_at": datetime.now(timezone.utc).isoformat()}
    return config


def stat_sample():
    return Obj(f_blocks=100, f_bfree=30, f_bavail=20, f_frsize=4096, f_files=1000, f_ffree=250)


def sample(config):
    return dm.collect(config, statvfs=lambda _: stat_sample(), ismount=lambda _: True)["metric_data"]


class DiskTests(unittest.TestCase):
    def setUp(self):
        # These samples model Linux mounts even when the test runner is Windows.
        realpath = patch.object(dm.os.path, "realpath", side_effect=lambda path: path)
        realpath.start()
        self.addCleanup(realpath.stop)
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("No real network in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_reserved_blocks_and_inode_values(self):
        values = dm.filesystem_values(stat_sample())
        self.assertEqual(values["DiskAvailableBytes"], 20 * 4096)
        self.assertAlmostEqual(values["DiskUsedPercent"], 100 * 70 / 90)
        self.assertEqual(values["InodeUsedPercent"], 75)

    def test_no_inodes_is_omitted_not_zero(self):
        value = stat_sample()
        value.f_files = 0
        result = dm.collect(synthetic_config(), statvfs=lambda _: value, ismount=lambda _: True)
        self.assertEqual(len(result["metric_data"]), 2)
        self.assertEqual(result["notes"], [{"mountpoint": "/", "code": "inode_unavailable"}])

    def test_missing_mount_does_not_sample_parent(self):
        config = synthetic_config() | {"mountpoints": ["/", "/synthetic-missing"]}
        stats = Mock(return_value=stat_sample())
        result = dm.collect(config, statvfs=stats, ismount=lambda path: path == "/")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["code"], "not_mounted")
        stats.assert_called_once_with("/")

    def test_all_missing_mounts_is_error(self):
        result = dm.collect(synthetic_config(), statvfs=Mock(side_effect=AssertionError("No sample")), ismount=lambda _: False)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["metric_data"], [])

    def test_mount_symlink_is_rejected(self):
        with patch.object(dm.os.path, "realpath", return_value="/different"):
            result = dm.collect(synthetic_config(), statvfs=Mock(side_effect=AssertionError("No sample")), ismount=lambda _: True)
        self.assertEqual(result["errors"][0]["code"], "mount_symlink")

    def test_linux_paths_can_be_planned_on_windows(self):
        config = synthetic_config() | {"mountpoints": ["/", "/data"]}
        with patch.object(dm, "os", Obj(path=ntpath)):
            dm.validate_config(config)
            self.assertEqual(dm.scope(config)["mountpoints"], ["/", "/data"])
            with self.assertRaises(dm.CollectorError) as caught:
                dm.collect(config)
            self.assertEqual(caught.exception.code, "unsupported_os")

    def test_invalid_names_mounts_and_batch_size_rejected(self):
        for changes in ({"namespace": "oci_custom"}, {"namespace": "oracle_secret"}, {"namespace": "a;bad"},
                        {"mountpoints": ["/data/../etc"]}, {"mountpoints": ["relative"]},
                        {"mountpoints": ["/", "/"]}, {"mountpoints": [f"/mount{i}" for i in range(17)]}):
            with self.subTest(changes=changes), self.assertRaises(dm.CollectorError):
                dm.validate_config(synthetic_config() | changes)

    def test_approval_required_and_scope_drift_rejected(self):
        with self.assertRaises(dm.CollectorError):
            dm.validate_approval(synthetic_config())
        config = approved_config()
        dm.validate_approval(config)
        for field, value in (("region", "us-phoenix-1"), ("namespace", "different"), ("mountpoints", ["/", "/data"]),
                             ("instance_id", "ocid1.instance.oc1.iad.other"), ("compartment_id", "ocid1.compartment.oc1..other")):
            with self.subTest(field=field), self.assertRaises(dm.CollectorError):
                dm.validate_approval(config | {field: value})

    def test_approval_time_needs_timezone_and_not_future(self):
        for stamp in (None, "2026-01-01T00:00:00", (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()):
            config = approved_config()
            config["publishing_approval"]["confirmed_at"] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(dm.CollectorError):
                dm.validate_approval(config)

    def test_default_and_describe_scope_never_touch_sdk_or_imds(self):
        for options in ([], ["--dry-run"], ["--describe-scope"]):
            output = io.StringIO()
            with patch.object(dm, "read_config", return_value=synthetic_config()), \
                    patch.object(dm, "collect", return_value={"status": "ok", "metric_data": [], "errors": []}) as collect, \
                    patch.object(dm, "publish", side_effect=AssertionError("Must not publish")), \
                    patch.object(dm, "verify_instance_identity", side_effect=AssertionError("Must not query IMDS")), redirect_stdout(output):
                self.assertEqual(dm.main(["--config", "/synthetic/config.json"]+options), 0)
            result = json.loads(output.getvalue())
            if options == ["--describe-scope"]:
                collect.assert_not_called()
                self.assertTrue(result["requires_user_confirmation"])
            else:
                self.assertEqual(result["mode"], "dry_run")

    def test_zero_failed_count_allows_absent_optional_details(self):
        for details in (Obj(failed_metrics_count=0), Obj(failed_metrics_count=0, failed_metrics=[])):
            self.assertEqual(dm.check_publish_response(Obj(data=details)), {"failed_metrics_count": 0})

    def test_response_failures_never_count_as_success(self):
        for details in (Obj(), Obj(failed_metrics_count=True), Obj(failed_metrics_count=1, failed_metrics=None),
                        Obj(failed_metrics_count=0, failed_metrics=[Obj()])):
            with self.subTest(details=details), self.assertRaises(dm.CollectorError):
                dm.check_publish_response(Obj(data=details))

    def test_imds_is_fixed_v2_bounded_and_identity_checked(self):
        config = synthetic_config()
        for payload in (config["instance_id"].encode(), json.dumps(config["instance_id"]).encode()):
            connection = Mock()
            connection.getresponse.return_value = Obj(status=200, read=Mock(return_value=payload))
            factory = Mock(return_value=connection)
            dm.verify_instance_identity(config["instance_id"], connection_factory=factory)
            factory.assert_called_once_with("169.254.169.254", timeout=3)
            connection.request.assert_called_once_with("GET", "/opc/v2/instance/id", headers={"Authorization": "Bearer Oracle"})
            connection.getresponse.return_value.read.assert_called_once_with(1025)
            connection.close.assert_called_once()

    def test_imds_wrong_instance_redirect_and_oversize_rejected(self):
        for status, payload in ((200, b"ocid1.instance.oc1.iad.wrong"), (302, b"redirect"), (200, b"x"*1025)):
            connection = Mock()
            connection.getresponse.return_value = Obj(status=status, read=Mock(return_value=payload))
            with self.subTest(status=status, size=len(payload)), self.assertRaises(dm.CollectorError):
                dm.verify_instance_identity(synthetic_config()["instance_id"], connection_factory=Mock(return_value=connection))
            connection.close.assert_called_once()

    def test_proxy_bypass_is_local_and_restored_even_on_failure(self):
        # Linux environment keys are case-sensitive; model them explicitly.
        with patch.object(dm.os, "environ", {"HTTP_PROXY": "http://proxy.invalid", "NO_PROXY": "existing.invalid"}):
            with self.assertRaises(RuntimeError):
                with dm.direct_metadata():
                    self.assertIn("169.254.169.254", os.environ["no_proxy"])
                    self.assertIn("fd00:c1::a9fe:a9fe", os.environ["NO_PROXY"])
                    self.assertIn("existing.invalid", os.environ["no_proxy"])
                    raise RuntimeError("synthetic")
            self.assertEqual(os.environ["NO_PROXY"], "existing.invalid")
            self.assertNotIn("no_proxy", os.environ)
            self.assertEqual(os.environ["HTTP_PROXY"], "http://proxy.invalid")

    def fake_sdk(self, region="us-ashburn-1", failures=0):
        client = Obj(post_metric_data=Mock(return_value=Obj(data=Obj(failed_metrics_count=failures), headers={})))
        sdk = Obj(auth=Obj(signers=Obj(InstancePrincipalsSecurityTokenSigner=Mock(return_value=Obj(region=region)))),
                  retry=Obj(NoneRetryStrategy=lambda: Obj()),
                  regions=Obj(endpoint_for=Mock(return_value="https://telemetry-ingestion.us-ashburn-1.oraclecloud.com")),
                  monitoring=Obj(MonitoringClient=Mock(return_value=client), models=Obj(
                      Datapoint=lambda **kw: Obj(**kw), MetricDataDetails=lambda **kw: Obj(**kw), PostMetricDataDetails=lambda **kw: Obj(**kw))))
        return sdk, client

    def test_publish_uses_ingestion_and_no_retry_after_scope_and_identity(self):
        sdk, client = self.fake_sdk()
        config = approved_config()
        with patch.dict(os.environ, {}, clear=True), patch.object(dm, "verify_instance_identity") as identity:
            result = dm.publish(config, sample(config), sdk=sdk)
        identity.assert_called_once_with(config["instance_id"])
        sdk.regions.endpoint_for.assert_called_once_with("monitoring", region=config["region"], service_endpoint_template=dm.INGESTION_TEMPLATE)
        self.assertEqual(client.post_metric_data.call_count, 1)
        details = client.post_metric_data.call_args.args[0]
        self.assertEqual(details.batch_atomicity, "ATOMIC")
        self.assertEqual(len(details.metric_data), 3)
        self.assertIn("retry_strategy", client.post_metric_data.call_args.kwargs)
        self.assertIn("federation_client_retry_strategy", sdk.auth.signers.InstancePrincipalsSecurityTokenSigner.call_args.kwargs)
        self.assertEqual(result, {"failed_metrics_count": 0})

    def test_publish_refuses_wrong_region_before_post(self):
        sdk, client = self.fake_sdk(region="us-phoenix-1")
        config = approved_config()
        with patch.dict(os.environ, {}, clear=True), patch.object(dm, "verify_instance_identity"), self.assertRaises(dm.CollectorError) as caught:
            dm.publish(config, sample(config), sdk=sdk)
        self.assertEqual(caught.exception.code, "region_mismatch")
        client.post_metric_data.assert_not_called()

    def test_metadata_override_is_rejected_before_signer(self):
        sdk, client = self.fake_sdk()
        config = approved_config()
        with patch.dict(os.environ, {"OCI_METADATA_BASE_URL": "http://untrusted.invalid"}), \
                patch.object(dm, "verify_instance_identity", side_effect=AssertionError("Must not query")), \
                self.assertRaises(dm.CollectorError) as caught:
            dm.publish(config, sample(config), sdk=sdk)
        self.assertEqual(caught.exception.code, "metadata_override")
        sdk.auth.signers.InstancePrincipalsSecurityTokenSigner.assert_not_called()

    def test_payload_cannot_change_approved_scope(self):
        config = approved_config()
        for field, value in (("compartment_id", "ocid1.compartment.oc1..other"), ("namespace", "other"), ("name", "OtherMetric")):
            data = sample(config)
            data[0][field] = value
            with self.subTest(field=field), self.assertRaises(dm.CollectorError):
                dm.validate_payload(config, data)
        for field, value in (("resourceId", "ocid1.instance.oc1.iad.other"), ("mountpoint", "/other")):
            data = sample(config)
            data[0]["dimensions"][field] = value
            with self.subTest(field=field), self.assertRaises(dm.CollectorError):
                dm.validate_payload(config, data)

    def test_stale_duplicate_invalid_values_rejected(self):
        config = approved_config()
        data = sample(config)
        with self.assertRaises(dm.CollectorError):
            dm.validate_payload(config, data+data)
        for value in (float("nan"), True, -1):
            data = sample(config)
            data[0]["datapoints"][0]["value"] = value
            with self.subTest(value=value), self.assertRaises(dm.CollectorError):
                dm.validate_payload(config, data)
        data = sample(config)
        data[0]["datapoints"][0]["timestamp"] = "2000-01-01T00:00:00Z"
        with self.assertRaises(dm.CollectorError):
            dm.validate_payload(config, data)

    def test_publish_error_does_not_echo_exception_or_retry(self):
        sdk, client = self.fake_sdk()
        client.post_metric_data.side_effect = RuntimeError("SYNTHETIC_PRIVATE_TOKEN_MUST_NOT_LEAK")
        config = approved_config()
        with patch.dict(os.environ, {}, clear=True), patch.object(dm, "verify_instance_identity"), self.assertRaises(dm.CollectorError) as caught:
            dm.publish(config, sample(config), sdk=sdk)
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", str(caught.exception))
        self.assertEqual(client.post_metric_data.call_count, 1)

    def test_real_sdk_models_endpoint_and_post_contract(self):
        try:
            import oci
        except ImportError:
            self.skipTest("Optional OCI SDK is not installed")
        class NoNetworkSigner(oci.auth.signers.SecurityTokenSigner):
            def __init__(self):
                pass
            def __call__(self, request):
                raise AssertionError("No signing or network in contract test")
        config = approved_config()
        data = sample(config)
        request = dm.make_request(oci, data)
        self.assertEqual(request.batch_atomicity, "ATOMIC")
        self.assertEqual(dm.ingestion_endpoint(oci, config["region"]),
                         "https://telemetry-ingestion.us-ashburn-1.oraclecloud.com")
        with patch("oci._vendor.requests.sessions.Session.request", side_effect=AssertionError("No HTTP")):
            client = oci.monitoring.MonitoringClient({"region": config["region"]}, signer=NoNetworkSigner(),
                       service_endpoint=dm.ingestion_endpoint(oci, config["region"]))
            wire = client.base_client.sanitize_for_serialization(request)
            self.assertEqual(wire["metricData"][0]["dimensions"], data[0]["dimensions"])
            self.assertEqual(wire["metricData"][0]["compartmentId"], config["compartment_id"])
            client.base_client.call_api = Mock(return_value=Obj(data=oci.monitoring.models.PostMetricDataResponseDetails(failed_metrics_count=0)))
            result = client.post_metric_data(request, retry_strategy=oci.retry.NoneRetryStrategy())
            self.assertEqual(dm.check_publish_response(result), {"failed_metrics_count": 0})
            self.assertEqual(client.base_client.call_api.call_count, 1)
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.invalid"}, clear=True), dm.direct_metadata():
            should_bypass = oci._vendor.requests.utils.should_bypass_proxies
            self.assertTrue(should_bypass("http://169.254.169.254/opc/v2/identity/key.pem", no_proxy=None))
            self.assertTrue(should_bypass("http://[fd00:c1::a9fe:a9fe]/opc/v2/identity/key.pem", no_proxy=None))


if __name__ == "__main__":
    unittest.main()
