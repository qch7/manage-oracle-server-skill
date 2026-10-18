"""Publication regressions with synthetic values; no network or credentials."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import validate


class PublicationTests(unittest.TestCase):
    def test_key_body_detected_without_echoing_material(self):
        material = "A" * 64
        text = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + material
        labels = validate.check_text(text)
        self.assertIn("private-key body", labels)
        self.assertNotIn(material, " ".join(labels))

    def test_personal_home_paths_detected(self):
        paths = ("C:" + "\\Users\\" + "example-user\\.ssh\\identity",
                 "/ho" + "me/example-user/.ssh/identity")
        for path in paths:
            with self.subTest(platform=path[:2]):
                self.assertIn("personal home path", validate.check_text(path))

    def test_long_identifier_and_token_detected(self):
        identifier = "ocid1.instance.oc1.example-region-1." + "a" * 60
        token = "ghp_" + "a" * 36
        self.assertIn("long OCI identifier", validate.check_text(identifier))
        self.assertIn("GitHub credential", validate.check_text(token))

    def test_placeholders_and_short_synthetic_identifiers_are_allowed(self):
        text = '<confirmed-native-absolute-key-path> ocid1.instance.oc1.test.synthetic'
        self.assertEqual(validate.check_text(text), [])

    def test_index_rejects_private_and_generated_files(self):
        for name in ("secret.key", "profiles.json", ".venv-sdk/bin/python", "bundle.zip",
                     "skill/state/record.json", "operation.plan.json", "scripts/__pycache__/module.pyc"):
            with self.subTest(name=name):
                self.assertTrue(validate.check_index_paths([name]))
        self.assertEqual(validate.check_index_paths(["README.md", "assets/disk-metrics.example.json"]), [])

    def test_links_and_json_examples(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "README.md"
            (root / "target.md").write_text("# Target", encoding="utf-8")
            self.assertEqual(validate.check_markdown(source, '[ok](target.md#target)\n```json\n{"ok": true}\n```', root), [])
            self.assertTrue(validate.check_markdown(source, '[missing](missing.md)', root))
            self.assertTrue(validate.check_markdown(source, '[outside](../outside.md)', root))
            self.assertTrue(validate.check_markdown(source, '```json\n{"broken": }\n```', root))


if __name__ == "__main__":
    unittest.main()
