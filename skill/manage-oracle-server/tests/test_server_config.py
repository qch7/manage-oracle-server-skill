"""Private-config risk tests; all files and identities are synthetic."""
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server_config as sc


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oracle-config-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        # Ignore ambient Git markers outside the synthetic fixture, while
        # exercising the actual production path gate on markers within it.
        exists = Path.exists
        def isolated_exists(path):
            if path.name == ".git" and self.root not in path.parents:
                return False
            return exists(path)
        self.addCleanup(patch.stopall)
        patch.object(Path, "exists", isolated_exists).start()
        self.config = self.root / "profiles.json"
        self.key = self.root / "synthetic-ssh-key"
        self.key.write_text("SYNTHETIC FILE; NEVER A PRIVATE KEY", encoding="utf-8")
        self.oci_file = self.root / "synthetic-oci-config"
        self.oci_file.write_text("SYNTHETIC CONFIG; NO CREDENTIALS", encoding="utf-8")
        self.stamp = datetime.now(timezone.utc).isoformat()
        self.ssh = {"host": "example.invalid", "port": 22, "user": "exampleuser",
                    "key_path": str(self.key), "key_path_confirmed_at": self.stamp}
        self.oci = {"oci": {"auth_mode": "api_key", "config_file": str(self.oci_file),
                          "config_profile": "SYNTHETIC", "region": "us-ashburn-1",
                          "instance_id": "ocid1.instance.oc1.iad.synthetic",
                          "compartment_id": "ocid1.compartment.oc1..synthetic", "confirmed_at": self.stamp}}

    def test_deep_merge_preserves_other_profiles_and_unknown_fields(self):
        original = {"version": 1, "notes": {"preserve": True}, "profiles": {
            "home": self.ssh | {"extension": {"first": 1}}, "other": {"opaque": [1, 2]}}}
        sc.atomic_json(self.config, original)
        sc.upsert_profile("home", self.oci | {"extension": {"second": 2}}, self.config)
        result = sc.load_config(self.config)
        self.assertEqual(result["profiles"]["other"], original["profiles"]["other"])
        self.assertEqual(result["notes"], original["notes"])
        self.assertEqual(result["profiles"]["home"]["extension"], {"first": 1, "second": 2})
        self.assertEqual(result["profiles"]["home"]["key_path"], str(self.key))

    def test_channels_are_independent(self):
        sc.upsert_profile("ssh", self.ssh, self.config)
        sc.upsert_profile("cloud", self.oci, self.config)
        sc.read_profile("ssh", self.config, need_ssh=True)
        sc.read_profile("cloud", self.config, need_oci=True)
        with self.assertRaises(sc.SkillError):
            sc.read_profile("ssh", self.config, need_oci=True)
        with self.assertRaises(sc.SkillError):
            sc.read_profile("cloud", self.config, need_ssh=True)

    def test_does_not_read_ssh_key_contents(self):
        sc.upsert_profile("home", self.ssh, self.config)
        read = Path.read_text
        def guarded(path, *args, **kwargs):
            if path == self.key:
                raise AssertionError("Private-key content must never be read")
            return read(path, *args, **kwargs)
        with patch.object(Path, "read_text", guarded):
            sc.read_profile("home", self.config, need_ssh=True)

    def test_changed_key_requires_new_confirmation(self):
        sc.upsert_profile("home", self.ssh, self.config)
        another = self.root / "another-synthetic-key"
        another.touch()
        with self.assertRaises(sc.SkillError) as caught:
            sc.upsert_profile("home", {"key_path": str(another)}, self.config)
        self.assertEqual(caught.exception.code, "confirmation_required")
        sc.upsert_profile("home", {"key_path": str(another),
                                  "key_path_confirmed_at": datetime.now(timezone.utc).isoformat()}, self.config)

    def test_changed_cloud_target_requires_new_confirmation(self):
        sc.upsert_profile("cloud", self.oci, self.config)
        with self.assertRaises(sc.SkillError) as caught:
            sc.upsert_profile("cloud", {"oci": {"region": "us-phoenix-1"}}, self.config)
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_changed_ssh_destination_requires_confirmation(self):
        sc.upsert_profile("home", self.ssh, self.config)
        for field, value in (("host", "other.invalid"), ("user", "otheruser"), ("port", 2222)):
            with self.subTest(field=field), self.assertRaises(sc.SkillError) as caught:
                sc.upsert_profile("home", {field: value}, self.config)
            self.assertEqual(caught.exception.code, "confirmation_required")

    def test_added_optional_oci_scope_requires_confirmation(self):
        sc.upsert_profile("cloud", self.oci, self.config)
        with self.assertRaises(sc.SkillError) as caught:
            sc.upsert_profile("cloud", {"oci": {"metric_compartment_id": "ocid1.compartment.oc1..other"}}, self.config)
        self.assertEqual(caught.exception.code, "confirmation_required")

    def test_add_cloud_channel_when_old_ssh_key_is_unavailable(self):
        sc.upsert_profile("home", self.ssh, self.config)
        self.key.unlink()  # Only this test's synthetic placeholder file.
        sc.upsert_profile("home", self.oci, self.config)
        sc.read_profile("home", self.config, need_oci=True)
        with self.assertRaises(sc.SkillError):
            sc.read_profile("home", self.config, need_ssh=True)

    def test_missing_or_future_confirmation_rejected(self):
        for stamp in (None, "2026-01-01T00:00:00", (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()):
            with self.subTest(stamp=stamp), self.assertRaises(sc.SkillError):
                sc.validate_ssh(self.ssh | {"key_path_confirmed_at": stamp})

    def test_host_user_and_port_injection_rejected(self):
        for field, value in (("host", "-oProxyCommand=bad"), ("host", "a;bad"),
                             ("host", "one two"), ("user", "me@elsewhere"),
                             ("user", "-root"), ("port", True), ("port", "22"), ("port", 0)):
            with self.subTest(field=field, value=value), self.assertRaises(sc.SkillError):
                sc.validate_ssh(self.ssh | {field: value})

    def test_ipv6_accepted_without_shell_syntax(self):
        sc.validate_ssh(self.ssh | {"host": "2001:db8::1"})
        with self.assertRaises(sc.SkillError):
            sc.validate_ssh(self.ssh | {"host": "fe80::1%bad-zone"})

    def test_relative_and_skill_paths_rejected(self):
        for path in ("relative.json", sc.SKILL_ROOT / "profiles.json"):
            with self.subTest(path=str(path)), self.assertRaises(sc.SkillError):
                sc.validate_private_path(path)

    def test_git_directory_and_worktree_marker_rejected(self):
        for dirname, as_file in (("repo", False), ("worktree", True)):
            folder = self.root / dirname
            folder.mkdir()
            marker = folder / ".git"
            marker.touch() if as_file else marker.mkdir()
            with self.assertRaises(sc.SkillError) as caught:
                sc.validate_private_path(folder / "nested" / "profiles.json")
            self.assertEqual(caught.exception.code, "unsafe_storage")

    def test_symlink_cannot_escape_storage_gate(self):
        repo = self.root / "linked-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        link = self.root / "alias"
        try:
            link.symlink_to(repo, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symlinks is unavailable for this test user")
        with self.assertRaises(sc.SkillError):
            sc.validate_private_path(link / "profiles.json")

    def test_duplicate_fields_and_unknown_versions_rejected(self):
        for text in ('{"version":1,"version":1,"profiles":{}}',
                     '{"version":true,"profiles":{}}', '{"version":2,"profiles":{}}'):
            self.config.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(sc.SkillError):
                sc.load_config(self.config)

    def test_utf8_bom_supported(self):
        self.config.write_text(json.dumps({"version": 1, "profiles": {"home": self.ssh}}), encoding="utf-8-sig")
        self.assertEqual(sc.read_profile("home", self.config, need_ssh=True)["host"], "example.invalid")

    def test_atomic_failure_preserves_original(self):
        sc.upsert_profile("home", self.ssh, self.config)
        before = self.config.read_bytes()
        with patch.object(sc.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                sc.upsert_profile("home", {"extension": True}, self.config)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.config.with_name("profiles.json.lock").exists())
        self.assertEqual(list(self.root.glob(".oracle-profile-*")), [])

    def test_exclusive_lock_preserves_pending_writer(self):
        sc.upsert_profile("home", self.ssh, self.config)
        with sc.config_lock(self.config):
            with self.assertRaises(sc.SkillError) as caught:
                sc.upsert_profile("home", {"extension": True}, self.config)
            self.assertEqual(caught.exception.code, "config_busy")
            self.assertTrue(self.config.with_name("profiles.json.lock").exists())

    def test_profile_return_is_detached_from_loaded_data(self):
        sc.upsert_profile("home", self.ssh, self.config)
        result = sc.read_profile("home", self.config)
        result["host"] = "changed.invalid"
        self.assertEqual(sc.read_profile("home", self.config)["host"], "example.invalid")

    def test_alias_is_required_and_not_inferred(self):
        sc.upsert_profile("home", self.ssh, self.config)
        for alias in (None, "", "../home", "missing"):
            with self.subTest(alias=alias), self.assertRaises(sc.SkillError):
                sc.read_profile(alias, self.config)


if __name__ == "__main__":
    unittest.main()
