"""Offline freshness and binding tests: fake sessions, no credentials or HTTP."""
from __future__ import annotations

import argparse
from collections import UserDict
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import oci_ops
import oci_verify
from server_config import SkillError

try:
    import oci as REAL_OCI
except ImportError:
    REAL_OCI = None


NOW = datetime(2026, 10, 18, 12, 0, 30, tzinfo=timezone.utc)
INSTANCE = "ocid1.instance.oc1.test.selected"
COMPARTMENT = "ocid1.compartment.oc1..instance"
METRIC_COMPARTMENT = "ocid1.compartment.oc1..metrics"
NAMESPACE = "custom_disk_monitoring"
SECRET = "SYNTHETIC_PRIVATE_KEY_AND_ERROR_BODY_NOT_FOR_OUTPUT"


def arguments(**updates):
    values = dict(action="verify", namespace=NAMESPACE,
                  metrics=["DiskUsedPercent", "DiskAvailableGiB"], mountpoints=["/"],
                  since=(NOW - timedelta(minutes=10)).isoformat(), minimum_points=2, max_age_minutes=3)
    values.update(updates)
    return argparse.Namespace(**values)


def point(minutes_ago=1, value=42, timestamp=None):
    return Obj(timestamp=NOW - timedelta(minutes=minutes_ago) if timestamp is None else timestamp, value=value)


def stream(metric="DiskUsedPercent", mount="/", points=None, **updates):
    fields = dict(namespace=NAMESPACE, name=metric, compartment_id=METRIC_COMPARTMENT,
                  dimensions={"resourceId": INSTANCE, "mountpoint": mount}, resource_group=None,
                  metadata={"description": SECRET},
                  aggregated_datapoints=[point(2), point(1)] if points is None else points)
    fields.update(updates)
    return Obj(**fields)


class ServiceFailure(Exception):
    def __init__(self, status):
        super().__init__(SECRET)
        self.status = status


class FakeMonitoring:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def summarize_metrics_data(self, compartment_id, summarize_metrics_data_details, **kwargs):
        details = summarize_metrics_data_details
        self.calls.append((compartment_id, details, kwargs))
        name = details.query.split("[", 1)[0]
        result = self.responses.get(name, [stream(name)])
        if isinstance(result, Exception):
            raise result
        if isinstance(result, Obj) and hasattr(result, "data"):
            return result
        return Obj(data=result, headers={})


class FakeSession:
    def __init__(self, responses=None, sdk=None):
        self.instance_id = INSTANCE
        self.compartment_id = COMPARTMENT
        self.region = "ap-osaka-1"
        self.settings = {"metric_compartment_id": METRIC_COMPARTMENT}
        self.sdk = sdk or Obj(monitoring=Obj(models=Obj(SummarizeMetricsDataDetails=Obj)))
        self.monitoring = FakeMonitoring(responses)
        self.ensured = 0
        self.client_names = []
        self.read_methods = []

    def ensure_instance(self):
        self.ensured += 1
        return Obj(id=INSTANCE, compartment_id=COMPARTMENT, region=self.region)

    def client(self, name):
        self.client_names.append(name)
        if name != "monitoring":
            raise AssertionError("Verification requested an unexpected client")
        return self.monitoring

    def call(self, method, *args, **kwargs):
        self.read_methods.append(method.__name__)
        if method.__name__ != "summarize_metrics_data":
            raise AssertionError("Verification requested an unexpected SDK operation")
        return method(*args, **kwargs)


class UnreadableSession:
    def __getattribute__(self, name):
        raise AssertionError("Invalid arguments must not read any session attribute")


def lookup(result, metric="DiskUsedPercent", mount="/"):
    return next(check for check in result["data"]["checks"]
                if check["metric"] == metric and check["mountpoint"] == mount)


def reason_codes(check):
    return {reason["code"] for reason in check["reasons"]}


class VerificationCase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(oci_verify, "_now", return_value=NOW))
        # Even accidental regression to networking or real session creation fails
        # locally instead of accessing HTTP, IMDS, user profiles, or private keys.
        self.stack.enter_context(patch("socket.socket", side_effect=AssertionError("HTTP/IMDS is forbidden")))
        self.stack.enter_context(patch.object(oci_ops, "OciSession", side_effect=AssertionError("Real sessions are forbidden")))
        self.stack.enter_context(patch.object(oci_ops, "read_profile", side_effect=AssertionError("Real profiles are forbidden")))
        if REAL_OCI is not None:
            self.stack.enter_context(patch.object(REAL_OCI.config, "from_file", side_effect=AssertionError("No OCI config reads")))
            self.stack.enter_context(patch.object(REAL_OCI.signer, "load_private_key_from_file", side_effect=AssertionError("No private key reads")))

    def run_verify(self, args=None, session=None):
        result = oci_verify.handle(args or arguments(), session or FakeSession())
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn(SECRET, encoded)
        self.assertEqual(set(result), {"status", "data", "errors", "coverage"})
        return result


class ArgumentTests(VerificationCase):
    def test_invalid_arguments_reject_before_any_session_attribute_read(self):
        invalid = [
            {"action": "publish"}, {"namespace": None}, {"namespace": "a" * 129},
            {"namespace": 'custom"}.mean()'}, {"namespace": "a\n"},
            {"metrics": []}, {"metrics": "DiskUsedPercent"}, {"metrics": ["A"] * 2},
            {"metrics": ["A", "B", "C", "D", "E"]}, {"metrics": [["A"]]},
            {"metrics": ['Disk[1m].mean() || Other[1m].max()']}, {"metrics": ["A\n"]},
            {"mountpoints": []}, {"mountpoints": ["/", "/"]}, {"mountpoints": "/"},
            {"mountpoints": [f"/{number}" for number in range(17)]}, {"mountpoints": [None]},
            {"minimum_points": 0}, {"minimum_points": 11}, {"minimum_points": True},
            {"minimum_points": 2.0}, {"max_age_minutes": 0}, {"max_age_minutes": 61},
            {"max_age_minutes": False}, {"max_age_minutes": float("nan")},
            {"max_age_minutes": float("inf")},
        ]
        for update in invalid:
            with self.subTest(update=update), self.assertRaises(SkillError):
                oci_verify.handle(arguments(**update), UnreadableSession())

    def test_noncanonical_linux_mountpoints_reject_on_every_host_os(self):
        invalid = ["data", "C:\\data", "\\\\server\\share", "//data", "//", "/data/",
                   "/data//x", "/./data", "/data/..", "/data/../x", "/data/./x", "/..",
                   "/x\n" , "/x\r", "/x\x00", "/x\x7f", "/x\x85", "/" + "x" * 4096]
        for mount in invalid:
            with self.subTest(mount=repr(mount)), self.assertRaises(SkillError):
                oci_verify.handle(arguments(mountpoints=[mount]), UnreadableSession())

    def test_since_is_strict_aware_past_and_bounded(self):
        invalid = [None, NOW, "2026-10-18T11:59:00", "2026-10-18", "2026-10-18 11:59:00Z",
                   "2026-10-18\n11:59:00Z", '2026-10-18T11:59:00Z"}.max()',
                   "2026-10-18T24:00:00Z", "2026-10-18T11:59:00+99:00",
                   "2026-10-18T11:59:00+00:99", "not-a-date",
                   "9999-12-31T23:59:59-09:00", "2026-10-18T11:59:00Z\n", "x" * 5000,
                   NOW.isoformat(), (NOW + timedelta(microseconds=1)).isoformat(),
                   (NOW - timedelta(hours=24, microseconds=1)).isoformat()]
        for since in invalid:
            with self.subTest(since=str(since)[:100]), self.assertRaises(SkillError):
                oci_verify.handle(arguments(since=since), UnreadableSession())

    def test_timezone_offsets_and_inclusive_oldest_allowed_boundary(self):
        options = oci_verify.validate_args(arguments(since="2026-10-18T20:50:30+09:00"))
        self.assertEqual(options.since, NOW - timedelta(minutes=10))
        oldest = NOW - timedelta(hours=24)
        self.assertEqual(oci_verify.validate_args(arguments(since=oldest.isoformat())).since, oldest)

    def test_maximum_unique_metrics_mounts_and_integer_bounds(self):
        for minimum, age in ((1, 1), (10, 60)):
            options = oci_verify.validate_args(arguments(
                metrics=["A", "B", "C", "D"], mountpoints=[f"/{number}" for number in range(16)],
                minimum_points=minimum, max_age_minutes=age))
            self.assertEqual((len(options.metrics), len(options.mountpoints)), (4, 16))

    def test_mount_filename_text_cannot_change_query(self):
        mount = '/data "} || Secret[1m].max(); $(echo synthetic)'
        session = FakeSession({"DiskUsedPercent": [stream(mount=mount)]})
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"], mountpoints=[mount]), session)
        self.assertEqual(result["status"], "ok")
        query = session.monitoring.calls[0][1].query
        self.assertEqual(query, f'DiskUsedPercent[1m]{{resourceId = "{INSTANCE}"}}.mean()')
        self.assertNotIn(mount, query)
        self.assertEqual(lookup(result, mount=mount)["mountpoint"], mount)


class FreshnessTests(VerificationCase):
    def test_legacy_gib_and_new_bytes_inode_metrics_at_each_mount(self):
        metrics = ["DiskUsedPercent", "DiskAvailableGiB", "DiskAvailableBytes", "InodeUsedPercent"]
        mounts = ["/", "/srv/project data"]
        session = FakeSession({metric: [stream(metric, mount) for mount in mounts] for metric in metrics})
        result = self.run_verify(arguments(metrics=metrics, mountpoints=mounts), session)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["verification"], "verified")
        self.assertEqual(result["data"]["check_counts"], {"total": 8, "verified": 8, "pending": 0, "incomplete": 0})
        self.assertEqual(session.ensured, 1)
        self.assertEqual(session.read_methods, ["summarize_metrics_data"] * 4)
        self.assertEqual(len(session.monitoring.calls), 4)
        for metric, (compartment, details, kwargs) in zip(metrics, session.monitoring.calls):
            self.assertEqual(compartment, METRIC_COMPARTMENT)
            self.assertEqual(kwargs, {"compartment_id_in_subtree": False})
            self.assertEqual(details.namespace, NAMESPACE)
            self.assertEqual(details.resolution, "1m")
            self.assertEqual(details.start_time, NOW - timedelta(minutes=6))
            self.assertEqual(details.end_time, NOW)
            self.assertEqual(details.query, f'{metric}[1m]{{resourceId = "{INSTANCE}"}}.mean()')
            for mount in mounts:
                check = lookup(result, metric, mount)
                self.assertEqual(check["fresh_minute_count"], 2)
                self.assertEqual(check["latest_point"]["timestamp"], "2026-10-18T11:59:30Z")
                self.assertEqual(check["latest_point"]["value"], 42.0)
        self.assertEqual(lookup(result, "DiskAvailableGiB")["latest_point"]["unit"], "GiB")
        self.assertEqual(lookup(result, "DiskAvailableBytes")["latest_point"]["unit"], "bytes")

    def test_query_window_uses_since_when_more_recent(self):
        since = NOW - timedelta(minutes=2, seconds=12)
        session = FakeSession()
        result = self.run_verify(arguments(since=since.isoformat()), session)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(session.monitoring.calls[0][1].start_time, since)

    def test_stopped_collector_cannot_pass_with_old_points_still_inside_window(self):
        session = FakeSession({"DiskUsedPercent": [stream(points=[point(5), point(4)])]})
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]), session)
        check = lookup(result)
        self.assertEqual(check["fresh_minute_count"], 2)
        self.assertEqual(check["latest_point"]["timestamp"], "2026-10-18T11:56:30Z")
        self.assertEqual(reason_codes(check), {"latest_point_too_old"})
        self.assertEqual((result["status"], result["data"]["verification"]), ("partial", "pending"))
        self.assertEqual(result["errors"], [])

    def test_before_since_at_since_before_window_and_future_points_never_count(self):
        points = [point(20), point(10), point(7), point(-1), point(1)]
        session = FakeSession({"DiskUsedPercent": [stream(points=points)]})
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]), session)
        check = lookup(result)
        self.assertEqual(check["fresh_minute_count"], 1)
        self.assertEqual(check["latest_point"]["timestamp"], "2026-10-18T11:59:30Z")
        self.assertEqual(check["excluded_datapoints"], {
            "not_after_since": 2, "before_query_window": 1, "future_timestamp": 1})
        self.assertEqual(reason_codes(check), {"insufficient_new_minutes"})

    def test_no_old_value_is_retimestamped_and_future_only_is_pending(self):
        for points in ([point(10), point(12)], [point(-1), point(-2)]):
            with self.subTest(points=points):
                result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                         FakeSession({"DiskUsedPercent": [stream(points=points)]}))
                self.assertEqual(lookup(result)["fresh_minute_count"], 0)
                self.assertIsNone(lookup(result)["latest_point"])
                self.assertEqual(result["data"]["verification"], "pending")

    def test_duplicate_utc_minute_counts_once_and_preserves_latest_value_time_pair(self):
        points = [point(value=10, timestamp="2026-10-18T20:59:10+09:00"),
                  point(value=30, timestamp="2026-10-18T11:59:55Z"),
                  point(value=20, timestamp="2026-10-18T11:59:20Z")]
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                 FakeSession({"DiskUsedPercent": [stream(points=points)]}))
        check = lookup(result)
        self.assertEqual(check["fresh_minute_count"], 1)
        self.assertEqual(check["excluded_datapoints"]["duplicate_minute"], 2)
        self.assertEqual(check["latest_point"], {"timestamp": "2026-10-18T11:59:55Z", "value": 30.0, "unit": "percent"})
        self.assertEqual(result["data"]["verification"], "pending")

    def test_at_now_and_maximum_age_boundary_are_inclusive(self):
        for stamp in (NOW, NOW - timedelta(minutes=3)):
            result = self.run_verify(arguments(metrics=["DiskUsedPercent"], minimum_points=1),
                                     FakeSession({"DiskUsedPercent": [stream(points=[point(timestamp=stamp)])]}))
            self.assertEqual(result["status"], "ok")

    def test_nonfinite_bool_string_huge_and_bad_timestamp_values_are_excluded(self):
        points = [point(value=number) for number in (float("inf"), float("-inf"), float("nan"), True, "42", None, 10 ** 1000)]
        points += [point(timestamp=NOW.replace(tzinfo=None)), point(timestamp="2026-10-18T11:59:00"),
                   point(timestamp="2026-10-18T11:59:00Z\n"), point(timestamp="invalid"), point(1)]
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                 FakeSession({"DiskUsedPercent": [stream(points=points)]}))
        check = lookup(result)
        self.assertEqual(check["fresh_minute_count"], 1)
        self.assertEqual(check["excluded_datapoints"]["invalid_or_nonfinite_value"], 7)
        self.assertEqual(check["excluded_datapoints"]["invalid_timestamp"], 4)
        self.assertEqual(result["data"]["verification"], "pending")

    def test_known_percent_and_capacity_ranges(self):
        for metric in ("DiskUsedPercent", "InodeUsedPercent", "DiskAvailableGiB", "DiskAvailableBytes"):
            with self.subTest(metric=metric):
                invalid_values = (-1, 101) if metric.endswith("Percent") else (-1, -0.1)
                points = [point(3 + number, value=entry) for number, entry in enumerate(invalid_values)]
                points += [point(2, value=0), point(1, value=100)]
                result = self.run_verify(arguments(metrics=[metric]), FakeSession({metric: [stream(metric, points=points)]}))
                self.assertEqual(result["status"], "ok")
                self.assertEqual(lookup(result, metric)["fresh_minute_count"], 2)
                self.assertEqual(lookup(result, metric)["excluded_datapoints"]["out_of_range_value"], 2)

    def test_other_named_metrics_allow_finite_negative_values(self):
        result = self.run_verify(arguments(metrics=["Temperature"]), FakeSession({
            "Temperature": [stream("Temperature", points=[point(2, value=-10), point(1, value=-8)])]}))
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(lookup(result, "Temperature")["latest_point"]["unit"])


class BindingAndCompletenessTests(VerificationCase):
    def test_wrong_instance_compartment_namespace_or_name_is_not_evidence(self):
        mismatches = [
            {"dimensions": {"resourceId": "ocid1.instance.oc1.test.other", "mountpoint": "/"}},
            {"compartment_id": COMPARTMENT}, {"compartment_id": None},
            {"namespace": "another_namespace"}, {"name": "DiskAvailableBytes"},
        ]
        for updates in mismatches:
            with self.subTest(updates=updates):
                result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                         FakeSession({"DiskUsedPercent": [stream(**updates)]}))
                check = lookup(result)
                self.assertEqual(check["fresh_minute_count"], 0)
                self.assertIsNone(check["latest_point"])
                self.assertEqual(check["verification"], "incomplete")
                self.assertIn("relationship_mismatch", reason_codes(check))
                self.assertEqual(result["status"], "partial")

    def test_unselected_mount_streams_are_skipped_without_inspecting_their_points(self):
        class UnreadablePoints:
            def __len__(self):
                raise AssertionError("Unselected points must not be inspected")
        ignored = stream(mount="/unselected", namespace="other", resource_group=SECRET,
                         aggregated_datapoints=UnreadablePoints(),
                         dimensions={"resourceId": "other-instance", "mountpoint": "/unselected", "extra": SECRET})
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                 FakeSession({"DiskUsedPercent": [ignored, stream()]}))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"][1]["skipped_unselected_streams"], 1)

    def test_extra_dimensions_and_nonempty_resource_group_are_ambiguous(self):
        for changes, reason in (
            ({"dimensions": {"resourceId": INSTANCE, "mountpoint": "/", "device": "x"}}, "ambiguous_dimensions"),
            ({"resource_group": "other-collector"}, "ambiguous_resource_group"),
        ):
            with self.subTest(changes=changes):
                result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                         FakeSession({"DiskUsedPercent": [stream(**changes)]}))
                check = lookup(result)
                self.assertTrue(check["ambiguous"])
                self.assertEqual(check["verification"], "incomplete")
                self.assertIn(reason, reason_codes(check))
                self.assertEqual(check["fresh_minute_count"], 0)
                self.assertIsNone(check["latest_point"])

    def test_none_or_empty_resource_group_is_unambiguous(self):
        for group in (None, ""):
            result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                     FakeSession({"DiskUsedPercent": [stream(resource_group=group)]}))
            self.assertEqual(result["status"], "ok")

    def test_two_streams_cannot_combine_individual_minutes_or_reuse_a_complete_stream(self):
        for streams in ([stream(points=[point(2)]), stream(points=[point(1)])], [stream(), stream(points=[])]):
            with self.subTest(stream_count=len(streams)):
                result = self.run_verify(arguments(metrics=["DiskUsedPercent"]), FakeSession({"DiskUsedPercent": streams}))
                check = lookup(result)
                self.assertEqual(check["stream_count"], 2)
                self.assertEqual(check["fresh_minute_count"], 0)
                self.assertIsNone(check["latest_point"])
                self.assertIn("ambiguous_streams", reason_codes(check))
                self.assertEqual(result["data"]["verification"], "incomplete")

    def test_one_ambiguous_mount_preserves_other_mount_success(self):
        streams = [stream(), stream(mount="/data"), stream(mount="/data")]
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"], mountpoints=["/", "/data"]),
                                 FakeSession({"DiskUsedPercent": streams}))
        self.assertEqual(lookup(result)["verification"], "verified")
        self.assertEqual(lookup(result, mount="/data")["verification"], "incomplete")
        self.assertEqual(result["status"], "partial")

    def test_missing_metric_is_pending_and_preserves_other_success(self):
        result = self.run_verify(session=FakeSession({"DiskAvailableGiB": []}))
        self.assertEqual(lookup(result)["verification"], "verified")
        missing = lookup(result, "DiskAvailableGiB")
        self.assertEqual(missing["verification"], "pending")
        self.assertIn("no_matching_stream", reason_codes(missing))
        self.assertEqual(result["data"]["verification"], "pending")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(row["complete"] for row in result["coverage"]))
        self.assertEqual(result["errors"], [])

    def test_403_is_incomplete_not_no_data_and_other_metric_is_retained(self):
        session = FakeSession({"DiskUsedPercent": ServiceFailure(403)})
        result = self.run_verify(session=session)
        check = lookup(result)
        self.assertEqual(check["verification"], "incomplete")
        self.assertEqual(reason_codes(check), {"oci_forbidden"})
        self.assertEqual(result["errors"][0]["http_status"], 403)
        self.assertEqual(lookup(result, "DiskAvailableGiB")["verification"], "verified")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(session.monitoring.calls), 2)

    def test_all_metric_requests_denied_are_error(self):
        result = self.run_verify(session=FakeSession({metric: ServiceFailure(403)
                                                      for metric in arguments().metrics}))
        self.assertEqual((result["status"], result["data"]["verification"]), ("error", "incomplete"))
        self.assertEqual(len(result["errors"]), 2)

    def test_target_check_failure_prevents_every_metric_request(self):
        session = FakeSession()
        with patch.object(session, "ensure_instance", side_effect=ServiceFailure(403)):
            result = self.run_verify(session=session)
        self.assertEqual(result["status"], "error")
        self.assertEqual(session.monitoring.calls, [])
        self.assertEqual(session.client_names, [])
        self.assertEqual(result["errors"][0]["code"], "oci_forbidden")
        self.assertTrue(all("target_unverified" in reason_codes(check) for check in result["data"]["checks"]))

    def test_instance_id_injection_and_invalid_compartment_cannot_reach_query(self):
        for field in ("instance_id", "metric_compartment_id"):
            session = FakeSession()
            if field == "instance_id":
                session.instance_id = 'ocid1.instance.oc1.x"} || Secret[1m].max()'
            else:
                session.settings[field] = "not-a-compartment"
            result = self.run_verify(session=session)
            self.assertEqual(result["status"], "error")
            self.assertEqual(session.monitoring.calls, [])

    def test_malformed_response_dimensions_or_points_is_incomplete(self):
        cases = [Obj(data=None, headers={}), Obj(data="not-a-list", headers={}),
                 [Obj(dimensions=None)], [stream(dimensions={"resourceId": INSTANCE})],
                 [stream(aggregated_datapoints=None)], [stream(aggregated_datapoints="invalid")]]
        for response in cases:
            with self.subTest(response=str(response)[:100]):
                result = self.run_verify(arguments(metrics=["DiskUsedPercent"]),
                                         FakeSession({"DiskUsedPercent": response}))
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["data"]["verification"], "incomplete")
                self.assertFalse(result["coverage"][1]["complete"])

    def test_mapping_headers_are_supported_and_continuation_is_not_followed(self):
        session = FakeSession({"DiskUsedPercent": Obj(data=[stream()], headers=UserDict({"opc-request-id": "synthetic"}))})
        self.assertEqual(self.run_verify(arguments(metrics=["DiskUsedPercent"]), session)["status"], "ok")
        session = FakeSession({"DiskUsedPercent": Obj(data=[stream()], headers=UserDict({"OPC-NEXT-PAGE": SECRET}))})
        result = self.run_verify(arguments(metrics=["DiskUsedPercent"]), session)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(lookup(result)["verification"], "incomplete")
        self.assertIn("unexpected_continuation", reason_codes(lookup(result)))
        self.assertEqual(len(session.monitoring.calls), 1)

    def test_stream_cap_cannot_claim_pass_from_truncated_evidence(self):
        streams = [stream(), stream(mount="/ignored"), stream()]
        with patch.object(oci_verify, "MAX_RESPONSE_STREAMS", 2):
            result = self.run_verify(session=FakeSession({"DiskUsedPercent": streams}))
        check = lookup(result)
        self.assertEqual(check["fresh_minute_count"], 2)
        self.assertEqual(check["verification"], "incomplete")
        self.assertIn("stream_limit", reason_codes(check))
        self.assertEqual(result["coverage"][1]["streams_examined"], 2)
        self.assertEqual(lookup(result, "DiskAvailableGiB")["verification"], "verified")
        self.assertEqual(result["status"], "partial")

    def test_point_cap_applies_to_whole_metric_and_cannot_claim_partial_stream_pass(self):
        streams = [stream(), stream(mount="/data")]
        with patch.object(oci_verify, "MAX_RESPONSE_POINTS", 3):
            result = self.run_verify(arguments(metrics=["DiskUsedPercent"], mountpoints=["/", "/data"]),
                                     FakeSession({"DiskUsedPercent": streams}))
        self.assertEqual(result["coverage"][1]["datapoints_examined"], 3)
        self.assertEqual([row["fresh_minute_count"] for row in result["data"]["checks"]], [2, 1])
        self.assertTrue(all(row["verification"] == "incomplete" for row in result["data"]["checks"]))
        self.assertEqual(result["data"]["verification"], "incomplete")

    def test_absent_metric_compartment_uses_only_bound_instance_compartment(self):
        session = FakeSession({metric: [stream(metric, compartment_id=COMPARTMENT)]
                               for metric in arguments().metrics})
        session.settings = {}
        result = self.run_verify(session=session)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["metric_compartment_id"], COMPARTMENT)
        self.assertTrue(all(call[0] == COMPARTMENT for call in session.monitoring.calls))


class CliAndSdkTests(VerificationCase):
    def cli(self, extra=None):
        argv = ["--server", "example", "monitoring", "verify", "--namespace", NAMESPACE,
                "--metrics", "DiskUsedPercent", "DiskAvailableGiB", "--mountpoints", "/",
                "--since", (NOW - timedelta(minutes=10)).isoformat()]
        return argv + (extra or [])

    def test_parser_exports_defaults_and_rejects_raw_mql_option(self):
        args = oci_ops.build_parser().parse_args(self.cli())
        self.assertEqual((args.action, args.minimum_points, args.max_age_minutes), ("verify", 2, 3))
        with self.assertRaises(SkillError):
            oci_ops.build_parser().parse_args(self.cli(["--query", "Anything[1m].mean()"]))

    def test_invalid_cli_rejects_before_profile_and_session_construction(self):
        for extra in (["--since", "2026-10-18T11:59:00"], ["--metrics", "A", "A"],
                      ["--mountpoints", "/", "/"], ["--max-age-minutes", "61"],
                      ["--since", NOW.isoformat()], ["--query", "A[1m].mean()"]):
            with self.subTest(extra=extra), patch.object(oci_ops, "read_profile") as read, \
                    patch.object(oci_ops, "OciSession") as constructor, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(oci_ops.main(self.cli(extra)), 1)
                read.assert_not_called()
                constructor.assert_not_called()
                self.assertEqual(json.loads(out.getvalue())["status"], "error")

    def test_valid_cli_dispatch_preserves_envelope_and_exit_codes(self):
        for session, expected in ((FakeSession(), 0),
                                  (FakeSession({"DiskAvailableGiB": []}), 2),
                                  (FakeSession({metric: ServiceFailure(403) for metric in arguments().metrics}), 1)):
            with self.subTest(expected=expected), \
                    patch.object(oci_ops, "read_profile", return_value={}) as read, \
                    patch.object(oci_ops, "OciSession", return_value=session) as constructor, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(oci_ops.main(self.cli()), expected)
                read.assert_called_once_with("example", None, need_oci=True)
                constructor.assert_called_once_with({})
                result = json.loads(out.getvalue())
                self.assertIn("checks", result["data"])
                self.assertIn("coverage", result)
                self.assertEqual(result["server"], "example")
                self.assertEqual(result["status"], {0: "ok", 2: "partial", 1: "error"}[expected])

    @unittest.skipUnless(REAL_OCI is not None, "Official OCI SDK not installed on this Python path")
    def test_official_sdk_models_offline_contract(self):
        models = REAL_OCI.monitoring.models
        rows = {}
        for metric in ("DiskUsedPercent", "DiskAvailableGiB", "DiskAvailableBytes"):
            rows[metric] = [models.MetricData(
                namespace=NAMESPACE, name=metric, compartment_id=METRIC_COMPARTMENT,
                dimensions={"resourceId": INSTANCE, "mountpoint": "/"}, resource_group="",
                aggregated_datapoints=[models.AggregatedDatapoint(timestamp=NOW - timedelta(minutes=number), value=42.0)
                                       for number in (2, 1)])]
        session = FakeSession(rows, sdk=REAL_OCI)
        result = self.run_verify(arguments(metrics=list(rows)), session)
        self.assertEqual(result["status"], "ok")
        for _, details, _ in session.monitoring.calls:
            self.assertIsInstance(details, models.SummarizeMetricsDataDetails)
            self.assertEqual(details.namespace, NAMESPACE)
            self.assertEqual(details.resolution, "1m")
            self.assertEqual(details.start_time, NOW - timedelta(minutes=6))


if __name__ == "__main__":
    unittest.main()
