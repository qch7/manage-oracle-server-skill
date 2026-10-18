"""Offline boundary tests. Synthetic OCIDs only; no credentials, SDK install or OCI calls."""
from __future__ import annotations

import argparse
import copy
from datetime import timedelta
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import oci_alarms as alarms
from server_config import SkillError


INSTANCE = "ocid1.instance.oc1.example-region-1.exampleinstance"
OTHER_INSTANCE = "ocid1.instance.oc1.example-region-1.otherinstance"
INSTANCE_COMP = "ocid1.compartment.oc1..exampleinstancecompartment"
ALARM_COMP = "ocid1.compartment.oc1..examplealarmcompartment"
METRIC_COMP = "ocid1.compartment.oc1..examplemetriccompartment"
TOPIC_COMP = "ocid1.compartment.oc1..exampletopiccompartment"
TOPIC = "ocid1.onstopic.oc1.example-region-1.exampletopic"
ALARM = "ocid1.alarm.oc1.example-region-1.examplealarm"


class ApiError(Exception):
    def __init__(self, status):
        super().__init__("secret-example-do-not-print-raw-sdk-errors")
        self.status = status


class NoRetry:
    pass


class Details:
    def __init__(self, **kwargs):
        self.swagger_types = {key: "value" for key in (*alarms.CORE_FIELDS, "freeform_tags")}
        for key in self.swagger_types:
            setattr(self, key, copy.deepcopy(kwargs.get(key)))


def response(data, etag="etag-1", page=None):
    headers = {"etag": etag}
    if page is not None:
        headers["opc-next-page"] = page
    return SimpleNamespace(data=data, headers=headers)


class Monitoring:
    def __init__(self):
        self.alarms = {}
        self.etags = {}
        self.create_calls = []
        self.update_calls = []
        self.list_calls = []
        self.page_size = 100
        self.error_status = None
        self.timeout_before_commit = False
        self.timeout_after_commit = False
        self.corrupt_created_field = None
        self.update_race = False
        self.fail_get = False

    def add_alarm(self, desired, identifier=ALARM, **extra):
        alarm = {key: None for key in alarms.PRESERVED_FIELDS}
        alarm.update(copy.deepcopy(desired), id=identifier, lifecycle_state="ACTIVE")
        alarm.update(extra)
        self.alarms[identifier] = alarm
        self.etags[identifier] = "etag-1"
        return identifier

    def list_alarms(self, compartment_id, **kwargs):
        self.list_calls.append((compartment_id, copy.deepcopy(kwargs)))
        items = [SimpleNamespace(**{k: v[k] for k in ("id", "display_name", "compartment_id", "lifecycle_state")})
                 for v in self.alarms.values()
                 if v["compartment_id"] == compartment_id and v["display_name"] == kwargs["display_name"]
                 and v["lifecycle_state"] == kwargs["lifecycle_state"]]
        start = int(kwargs.get("page", 0))
        chunk = items[start:start + self.page_size]
        following = str(start + self.page_size) if start + self.page_size < len(items) else None
        return response(chunk, page=following)

    def get_alarm(self, identifier):
        if self.fail_get:
            raise ApiError(500)
        if identifier not in self.alarms:
            raise ApiError(404)
        return response(copy.deepcopy(self.alarms[identifier]), self.etags[identifier])

    def create_alarm(self, details, **kwargs):
        if set(kwargs) != {"opc_retry_token", "retry_strategy"} or not isinstance(kwargs["retry_strategy"], NoRetry):
            raise AssertionError("Only supported create kwargs and explicit no-retry are allowed")
        payload = {k: copy.deepcopy(getattr(details, k)) for k in details.swagger_types
                   if getattr(details, k) is not None}
        self.create_calls.append((payload, kwargs))
        if self.error_status:
            raise ApiError(self.error_status)
        if self.timeout_before_commit:
            raise TimeoutError("secret-example-do-not-print-network-errors")
        identifier = ALARM if ALARM not in self.alarms else ALARM + str(len(self.alarms))
        self.add_alarm(payload, identifier)
        if self.corrupt_created_field:
            self.alarms[identifier][self.corrupt_created_field] = "unexpected"
        if self.timeout_after_commit:
            raise TimeoutError("secret-example-do-not-print-network-errors")
        return response(copy.deepcopy(self.alarms[identifier]), self.etags[identifier])

    def update_alarm(self, identifier, details, **kwargs):
        if set(kwargs) != {"if_match", "retry_strategy"} or not isinstance(kwargs["retry_strategy"], NoRetry):
            raise AssertionError("Update must not receive unsupported opc_retry_token")
        payload = {k: copy.deepcopy(getattr(details, k)) for k in details.swagger_types
                   if getattr(details, k) is not None}
        self.update_calls.append((identifier, payload, kwargs))
        if self.update_race:
            self.etags[identifier] = "etag-racing-writer"
        if kwargs["if_match"] != self.etags[identifier]:
            raise ApiError(412)
        if self.error_status:
            raise ApiError(self.error_status)
        if self.timeout_before_commit:
            raise TimeoutError()
        self.alarms[identifier].update(payload)
        self.etags[identifier] = "etag-2"
        if self.timeout_after_commit:
            raise TimeoutError()
        return response(None, self.etags[identifier])


class Notifications:
    def __init__(self):
        self.topic = {"topic_id": TOPIC, "name": "example-operations", "compartment_id": TOPIC_COMP,
                      "lifecycle_state": "ACTIVE"}
        self.etag = "topic-etag-1"
        self.reads = 0

    def get_topic(self, identifier):
        self.reads += 1
        if identifier != self.topic["topic_id"]:
            raise ApiError(404)
        return response(copy.deepcopy(self.topic), self.etag)


class Session:
    def __init__(self):
        self.instance_id, self.compartment_id, self.region = INSTANCE, INSTANCE_COMP, "example-region-1"
        self.instance = {"id": INSTANCE, "compartment_id": INSTANCE_COMP, "lifecycle_state": "RUNNING"}
        self.monitoring = Monitoring()
        self.notifications = Notifications()
        self.ensure_calls = 0
        self.sdk = SimpleNamespace(monitoring=SimpleNamespace(models=SimpleNamespace(
            CreateAlarmDetails=Details, UpdateAlarmDetails=Details)), retry=SimpleNamespace(NoneRetryStrategy=NoRetry))

    def client(self, name):
        return {"monitoring": self.monitoring, "notification_control": self.notifications}[name]

    def ensure_instance(self):
        self.ensure_calls += 1
        return copy.deepcopy(self.instance)


class AlarmTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="oci-alarm-offline-")
        self.root = Path(self.folder.name).resolve()
        # Hosted test environments may place /tmp inside a Git fixture. This suite
        # isolates the separate server_config path policy with a fixture-only gate;
        # the replacement cannot address files outside this synthetic directory.
        gate = patch.object(alarms, "validate_private_path", side_effect=self.fixture_path)
        gate.start()
        self.addCleanup(gate.stop)
        self.session = Session()
        self.spec = {
            "kind": "threshold", "display_name": "example-cpu-high", "namespace": "oci_computeagent",
            "metric": "CpuUtilization", "statistic": "mean", "comparison": ">=", "threshold": 85,
            "pending_duration": "PT5M", "compartment_id": ALARM_COMP,
            "metric_compartment_id": METRIC_COMP, "topic_id": TOPIC,
            "description": "Example alarm summary", "body": "Inspect workload before changing services.",
        }
        self.spec_path = self.root / "spec.json"
        self.plan_path = self.root / "plan.json"

    def tearDown(self):
        self.folder.cleanup()

    def fixture_path(self, value, must_exist=False, kind="file"):
        path = Path(value)
        if not path.is_absolute() or self.root not in path.resolve().parents:
            raise SkillError("unsafe_storage", "Test fixture path must remain isolated")
        if must_exist and not path.is_file():
            raise SkillError("missing_file", "Synthetic fixture file is missing")
        return path.resolve()

    def plan(self, spec=None):
        self.spec_path.write_text(json.dumps(spec or self.spec), encoding="utf-8")
        result = alarms.handle(SimpleNamespace(action="plan-alarm", server="example", spec=str(self.spec_path),
                                                out=str(self.plan_path)), self.session)
        self.document = json.loads(self.plan_path.read_text(encoding="utf-8"))
        self.digest = result["plan_sha256"]
        return result

    def apply(self, approval=True, path=None):
        value = self.digest if approval is True else approval
        return alarms.handle(SimpleNamespace(action="apply-alarm", server="example", plan=str(path or self.plan_path),
                                                approve_plan=value), self.session)

    def assert_code(self, expected, callable_, *args, **kwargs):
        with self.assertRaises(SkillError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)
        self.assertNotIn("secret-example", caught.exception.message)
        return caught.exception

    def prepare_update(self, **extra):
        before = alarms.normalize_spec(dict(self.spec, threshold=70))
        desired = alarms._desired(before, INSTANCE, "a" * 32)
        self.session.monitoring.add_alarm(desired, **extra)
        self.spec["alarm_id"] = ALARM
        return self.plan()

    def test_plan_performs_no_cloud_mutation_and_shows_exact_scope(self):
        result = self.plan()
        review = result["review"]
        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(review["resourceId"], INSTANCE)
        self.assertEqual(review["alarm_compartment_id"], ALARM_COMP)
        self.assertEqual(review["metric_compartment_id"], METRIC_COMP)
        self.assertEqual(review["topic"]["compartment_id"], TOPIC_COMP)
        self.assertEqual(review["threshold"], 85)
        self.assertEqual(review["pending_duration"], "PT5M")
        self.assertIn("body", {c["field"] for c in review["changes"]})
        self.assertFalse(result["cloud_write_attempted"])
        self.assertEqual(self.session.monitoring.create_calls, [])
        self.assertEqual(self.session.monitoring.update_calls, [])

    def test_missing_approval_refuses_before_any_session_read(self):
        self.plan()
        before = self.session.ensure_calls
        with patch("builtins.input", side_effect=AssertionError("Must not prompt in scripts")):
            self.assert_code("alarm_approval_required", self.apply, approval=None)
        self.assertEqual(self.session.ensure_calls, before)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_old_approval_cannot_execute_tampered_plan(self):
        self.plan()
        self.document["plan"]["spec"]["threshold"] = 90
        self.plan_path.write_text(json.dumps(self.document), encoding="utf-8")
        self.assert_code("alarm_plan_hash_mismatch", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_rehashed_arbitrary_query_still_refused(self):
        self.plan()
        self.document["plan"]["desired"]["query"] = "CpuUtilization[1m].mean() > 1"
        self.document["sha256"] = alarms._digest(self.document["plan"])
        self.digest = self.document["sha256"]
        self.plan_path.write_text(json.dumps(self.document), encoding="utf-8")
        self.assert_code("invalid_alarm_plan", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_expired_plan_refuses_even_with_correct_hash(self):
        self.plan()
        now = alarms._now()
        with patch.object(alarms, "_now", return_value=now + timedelta(minutes=16)):
            self.assert_code("alarm_plan_expired", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_plan_expiring_during_preflight_is_not_written(self):
        self.plan()
        initial = alarms._now()
        clock = [initial]
        original = self.session.monitoring.list_alarms
        def delayed_read(*args, **kwargs):
            clock[0] = initial + timedelta(minutes=16)
            return original(*args, **kwargs)
        with patch.object(alarms, "_now", side_effect=lambda: clock[0]), patch.object(
                self.session.monitoring, "list_alarms", side_effect=delayed_read):
            self.assert_code("alarm_plan_expired", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])
        self.assertFalse(Path(str(self.plan_path) + ".journal.json").exists())

    def test_no_overwrite_of_prior_plan_or_journal(self):
        self.plan()
        self.assert_code("alarm_plan_exists", self.plan)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_same_name_is_refused_for_create_and_deleting_resources(self):
        desired = alarms._desired(alarms.normalize_spec(self.spec), INSTANCE, "a" * 32)
        self.session.monitoring.add_alarm(desired, lifecycle_state="DELETING")
        self.assert_code("duplicate_alarm_name", self.plan)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_multiple_matching_names_on_later_page_refuse_update(self):
        desired = alarms._desired(alarms.normalize_spec(self.spec), INSTANCE, "a" * 32)
        self.session.monitoring.add_alarm(desired)
        self.session.monitoring.add_alarm(desired, identifier=ALARM + "second")
        self.session.monitoring.page_size = 1
        self.spec["alarm_id"] = ALARM
        self.assert_code("duplicate_alarm_name", self.plan)
        self.assertTrue(any("page" in kw for _, kw in self.session.monitoring.list_calls))

    def test_pagination_cycle_is_not_treated_as_absent_alarm(self):
        self.session.monitoring.list_alarms = lambda *a, **kw: response([], page="same-cursor")
        self.assert_code("incomplete_alarm_list", self.plan)

    def test_query_uses_bound_instance_and_mountpoint_group(self):
        self.spec.update(namespace="example_guest", metric="FilesystemUsedPercent", statistic="max", mountpoint="/var/lib")
        review = self.plan()["review"]
        self.assertEqual(review["query"], f'FilesystemUsedPercent[1m]{{resourceId = "{INSTANCE}", mountpoint = "/var/lib"}}.groupBy(resourceId, mountpoint).max() >= 85')
        self.assertEqual(review["mountpoint"], "/var/lib")

    def test_unsupported_query_and_dimension_injection_are_refused(self):
        for addition in ({"query": "CpuUtilization[1m].mean() > 1"}, {"resourceId": OTHER_INSTANCE},
                         {"mountpoint": '/"} || Any[1m].mean() > 0'}, {"metric": "CpuUtilization[1m]"}):
            with self.subTest(addition=addition):
                self.assert_code("invalid_alarm_spec", alarms.normalize_spec, dict(self.spec, **addition))

    def test_absence_detection_period_is_separate_from_trigger_delay(self):
        spec = {k: v for k, v in self.spec.items() if k not in ("statistic", "comparison", "threshold")}
        spec.update(kind="absence", absence_detection_period="20m", pending_duration="PT5M")
        review = self.plan(spec)["review"]
        self.assertIn(".absent(20m)", review["query"])
        self.assertEqual(review["pending_duration"], "PT5M")
        self.assertEqual(review["absence_detection_period"], "20m")
        self.assertIn("不能当作触发延迟", review["trigger_timing"])

    def test_validation_bounds_and_no_secret_or_control_characters(self):
        variants = [{"pending_duration": "PT0M"}, {"pending_duration": "PT61M"}, {"pending_duration": "P1D"},
                    {"threshold": True}, {"threshold": float("inf")}, {"threshold": 10**500},
                    {"kind": []}, {"statistic": {}}, {"is_enabled": "true"}, {"severity": []},
                    {"body": "line\nline"}, {"body": "token\u202eexample"}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.assert_code("invalid_alarm_spec", alarms.normalize_spec, dict(self.spec, **changes))
        self.assert_code("secret_in_alarm_text", alarms.normalize_spec, dict(self.spec, body="password=example-secret"))
        self.assertEqual(alarms.normalize_spec(dict(self.spec, pending_duration="PT1H"))["pending_duration"], "PT60M")

    def test_float_threshold_is_not_rounded_in_mql(self):
        value = 85.00000000000001
        spec = alarms.normalize_spec(dict(self.spec, threshold=value))
        self.assertTrue(alarms.build_query(spec, INSTANCE).endswith(str(value)))

    def test_json_duplicate_keys_and_nonfinite_numbers_are_refused(self):
        for raw in ('{"kind":"threshold","kind":"absence"}', '{"threshold":NaN}', '{"threshold":1e999}'):
            self.spec_path.write_text(raw, encoding="utf-8")
            self.assert_code("invalid_alarm_json", alarms._read_json, self.spec_path)

    def test_target_or_topic_changes_invalidate_plan(self):
        self.plan()
        self.session.instance["lifecycle_state"] = "STOPPED"
        self.assert_code("alarm_plan_stale", self.apply)
        self.session.instance["lifecycle_state"] = "RUNNING"
        self.session.notifications.etag = "topic-etag-2"
        self.assert_code("alarm_plan_stale", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_create_then_repeated_apply_is_read_only(self):
        self.plan()
        self.assertEqual(self.apply()["status"], "applied")
        self.assertEqual(self.apply()["status"], "already_applied")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)
        self.assertEqual(self.session.monitoring.create_calls[0][1]["opc_retry_token"], self.document["plan"]["plan_id"])

    def test_copied_plan_can_reconcile_only_its_own_complete_result(self):
        self.plan()
        self.apply()
        copied = self.root / "copied-plan.json"
        shutil.copyfile(self.plan_path, copied)
        self.assertEqual(self.apply(path=copied)["status"], "already_applied")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)

    def test_same_name_with_different_plan_marker_cannot_be_adopted(self):
        self.plan()
        desired = copy.deepcopy(self.document["plan"]["desired"])
        desired["freeform_tags"][alarms.PLAN_TAG] = "b" * 32
        self.session.monitoring.add_alarm(desired)
        self.assert_code("duplicate_alarm_name", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_unknown_create_result_is_not_retried(self):
        self.plan()
        self.session.monitoring.timeout_before_commit = True
        first = self.apply()
        second = self.apply()
        self.assertEqual(first["status"], "uncertain")
        self.assertEqual(second["status"], "uncertain")
        self.assertTrue(first["cloud_write_attempted"])
        self.assertFalse(second["cloud_write_attempted"])
        self.assertEqual(len(self.session.monitoring.create_calls), 1)
        self.assertNotIn("secret-example", json.dumps(first))

    def test_timeout_after_create_is_verified_by_full_get(self):
        self.plan()
        self.session.monitoring.timeout_after_commit = True
        result = self.apply()
        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["alarm_id"], ALARM)
        self.assertEqual(self.apply()["status"], "already_applied")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)

    def test_name_alone_does_not_prove_timeout_success(self):
        self.plan()
        self.session.monitoring.timeout_after_commit = True
        self.session.monitoring.corrupt_created_field = "namespace"
        self.assertEqual(self.apply()["status"], "uncertain")
        self.assertEqual(self.apply()["status"], "uncertain")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)

    def test_crash_receipt_allows_only_read_only_recovery(self):
        self.plan()
        receipt_path = Path(str(self.plan_path) + ".journal.json")
        alarms._receipt(receipt_path, self.digest, "started")
        self.assertEqual(self.apply()["status"], "uncertain")
        self.session.monitoring.add_alarm(self.document["plan"]["desired"])
        self.assertEqual(self.apply()["status"], "recovered")
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_residual_lock_is_never_automatically_broken(self):
        self.plan()
        lock = Path(str(self.plan_path) + ".lock")
        lock.write_text('{"created_at":"2001-01-01T00:00:00Z"}', encoding="utf-8")
        self.assert_code("alarm_apply_locked", self.apply)
        self.assertTrue(lock.exists())
        self.assertEqual(self.session.monitoring.create_calls, [])

    def test_definite_api_rejection_is_safe_and_not_retried(self):
        self.plan()
        self.session.monitoring.error_status = 403
        self.assertEqual(self.apply()["status"], "failed")
        second = self.apply()
        self.assertEqual(second["status"], "failed")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)
        self.assertNotIn("secret-example", json.dumps(second))

    def test_update_requires_explicit_id_preserves_tags_and_hides_old_text(self):
        self.prepare_update(body="password=old-example-private-body", freeform_tags={"private-note": "old-example-private-tag"})
        contents = self.plan_path.read_text(encoding="utf-8")
        self.assertNotIn("old-example-private-body", contents)
        self.assertNotIn("old-example-private-tag", contents)
        self.assertEqual(self.apply()["status"], "applied")
        self.assertEqual(self.session.monitoring.alarms[ALARM]["freeform_tags"], {"private-note": "old-example-private-tag"})
        self.assertEqual(len(self.session.monitoring.update_calls), 1)
        self.assertEqual(self.session.monitoring.update_calls[0][2]["if_match"], "etag-1")

    def test_unrelated_alarm_id_or_composite_query_cannot_be_overwritten(self):
        desired = alarms._desired(alarms.normalize_spec(self.spec), OTHER_INSTANCE, "a" * 32)
        self.session.monitoring.add_alarm(desired)
        self.spec["alarm_id"] = ALARM
        self.assert_code("unrelated_alarm", self.plan)
        self.session.monitoring.alarms[ALARM]["query"] = alarms.build_query(alarms.normalize_spec(self.spec), INSTANCE) + " || OtherMetric[1m].mean() > 0"
        self.assert_code("unrelated_alarm", self.plan)
        self.assertEqual(self.session.monitoring.update_calls, [])

    def test_etag_change_blocks_update_before_write(self):
        self.prepare_update()
        self.session.monitoring.etags[ALARM] = "etag-another-writer"
        self.assert_code("alarm_plan_stale", self.apply)
        self.assertEqual(self.session.monitoring.update_calls, [])

    def test_if_match_handles_race_after_preflight(self):
        self.prepare_update()
        self.session.monitoring.update_race = True
        result = self.apply()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["code"], "alarm_conflict")
        self.assertEqual(len(self.session.monitoring.update_calls), 1)
        self.assertIn(">= 70", self.session.monitoring.alarms[ALARM]["query"])

    def test_update_timeout_recovery_does_not_submit_again(self):
        self.prepare_update()
        self.session.monitoring.timeout_after_commit = True
        self.assertEqual(self.apply()["status"], "recovered")
        self.assertEqual(self.apply()["status"], "already_applied")
        self.assertEqual(len(self.session.monitoring.update_calls), 1)

    def test_sdk_silent_field_omission_fails_before_mutation(self):
        class OldDetails(Details):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.swagger_types.pop("alarm_summary")
                self.alarm_summary = None
        self.plan()
        self.session.sdk.monitoring.models.CreateAlarmDetails = OldDetails
        self.assert_code("unsupported_oci_sdk", self.apply)
        self.assertEqual(self.session.monitoring.create_calls, [])
        self.assertFalse(Path(str(self.plan_path) + ".journal.json").exists())

    def test_partial_local_receipt_failure_is_explicit(self):
        self.plan()
        original = alarms._write_json
        def failing_receipt(path, data, **kwargs):
            if data.get("state") == "succeeded":
                raise SkillError("alarm_local_state_failed", "synthetic receipt failure")
            return original(path, data, **kwargs)
        with patch.object(alarms, "_write_json", side_effect=failing_receipt):
            result = self.apply()
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["alarm_id"], ALARM)
        self.assertEqual(self.apply()["status"], "recovered")
        self.assertEqual(len(self.session.monitoring.create_calls), 1)

    def test_cli_only_exposes_plan_and_apply(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="action", required=True)
        alarms.add_subparsers(sub)
        self.assertEqual(set(sub.choices), {"plan-alarm", "apply-alarm"})
        args = parser.parse_args(["apply-alarm", "--plan", str(self.plan_path)])
        self.assertIsNone(args.approve_plan)


class InstalledSdkContractTests(unittest.TestCase):
    def test_real_models_serialization_and_write_kwargs_without_network(self):
        """Runs when the user's chosen environment already has the official SDK."""
        try:
            import oci
        except ImportError:
            self.skipTest("Optional OCI SDK is not installed; fake-client tests remain fully offline")

        class NoNetworkSigner(oci.auth.signers.SecurityTokenSigner):
            def __init__(self):
                pass  # A test object: do not read any key, token, config or metadata.

            def __call__(self, request):
                raise AssertionError("Signing/network access is forbidden in the contract test")

        raw = {"kind": "threshold", "display_name": "example-sdk-contract", "namespace": "oci_computeagent",
               "metric": "CpuUtilization", "statistic": "mean", "comparison": ">=", "threshold": 85,
               "pending_duration": "PT5M", "compartment_id": ALARM_COMP,
               "metric_compartment_id": METRIC_COMP, "topic_id": TOPIC}
        captured = []

        def capture(*args, **kwargs):
            captured.append(kwargs)
            return response(None)

        with patch("oci._vendor.requests.sessions.Session.request", side_effect=AssertionError("No network")):
            client = oci.monitoring.MonitoringClient({"region": "us-ashburn-1"}, signer=NoNetworkSigner())
            client.base_client.call_api = capture
            for operation, model_class in (("create", oci.monitoring.models.CreateAlarmDetails),
                                           ("update", oci.monitoring.models.UpdateAlarmDetails)):
                with self.subTest(operation=operation):
                    spec = dict(raw, **({"alarm_id": ALARM} if operation == "update" else {}))
                    desired = alarms._desired(alarms.normalize_spec(spec), INSTANCE, "a" * 32)
                    model = model_class(**desired)
                    wire = client.base_client.sanitize_for_serialization(model)
                    for key, value in desired.items():
                        self.assertIn(key, model.swagger_types)
                        self.assertEqual(oci.util.to_dict(model)[key], value)
                        self.assertEqual(wire[model.attribute_map[key]], value)
                    if operation == "create":
                        client.create_alarm(model, opc_retry_token="a" * 32, retry_strategy=oci.retry.NoneRetryStrategy())
                        self.assertEqual(captured[-1]["header_params"]["opc-retry-token"], "a" * 32)
                        self.assertNotIn("if-match", captured[-1]["header_params"])
                    else:
                        client.update_alarm(ALARM, model, if_match="reviewed-etag", retry_strategy=oci.retry.NoneRetryStrategy())
                        self.assertEqual(captured[-1]["header_params"]["if-match"], "reviewed-etag")
                        self.assertNotIn("opc-retry-token", captured[-1]["header_params"])
        self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()
