import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from assessment.check_scope import check, MANIFEST
from assessment.prepare_submission import prepare


class ScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.base = self.root / "base"
        self.base.mkdir()
        (self.base / "api").mkdir()
        (self.base / "api/fixed.py").write_text("trusted framework\n")
        (self.base / "solution").mkdir()
        (self.base / "solution/agent.py").write_text("candidate entry\n")
        (self.base / "assessment").mkdir()
        manifest = {"mutable": ["solution/**"], "required": ["solution/agent.py"],
                    "protected": {"api/fixed.py": hashlib.sha256((self.base / "api/fixed.py").read_bytes()).hexdigest()}}
        (self.base / MANIFEST).write_text(json.dumps(manifest))
        self.candidate = self.root / "candidate"
        shutil.copytree(self.base, self.candidate)

    def test_allowed_solution_change_passes(self):
        (self.candidate / "solution/agent.py").write_text("new implementation\n")
        (self.candidate / "solution/helper.py").write_text("helper\n")
        self.assertTrue(check(self.candidate, self.base / MANIFEST)["ok"])

    def test_fixed_code_change_is_rejected(self):
        (self.candidate / "api/fixed.py").write_text("pretend success\n")
        self.assertFalse(check(self.candidate, self.base / MANIFEST)["ok"])

    def test_modified_manifest_cannot_override_instructor_manifest(self):
        (self.candidate / "api/fixed.py").write_text("changed\n")
        manifest = json.loads((self.candidate / MANIFEST).read_text())
        manifest["protected"]["api/fixed.py"] = hashlib.sha256((self.candidate / "api/fixed.py").read_bytes()).hexdigest()
        (self.candidate / MANIFEST).write_text(json.dumps(manifest))
        result = check(self.candidate, self.base / MANIFEST)
        self.assertFalse(result["ok"])
        self.assertTrue(any(item["reason"] == "manifest_changed" for item in result["problems"]))

    def test_new_out_of_scope_file_is_rejected(self):
        (self.candidate / "override.py").write_text("override\n")
        self.assertFalse(check(self.candidate, self.base / MANIFEST)["ok"])

    def test_symlink_is_rejected(self):
        (self.candidate / "solution/link.py").symlink_to(self.base / "api/fixed.py")
        self.assertFalse(check(self.candidate, self.base / MANIFEST)["ok"])

    def test_prepared_copy_uses_fixed_core_and_excludes_credentials(self):
        (self.candidate / "solution/agent.py").write_text("new implementation\n")
        (self.candidate / ".env.models").write_text("MODEL_KEY=not-to-be-copied\n")
        output = self.root / "evaluation"
        result = prepare(self.base, self.candidate, output)
        self.assertTrue(result["ok"])
        self.assertEqual((output / "api/fixed.py").read_text(), "trusted framework\n")
        self.assertEqual((output / "solution/agent.py").read_text(), "new implementation\n")
        self.assertFalse((output / ".env.models").exists())
        self.assertIn("!reset []", (output / "compose.eval.yaml").read_text())

    def test_existing_destination_is_not_overwritten(self):
        output = self.root / "evaluation"
        output.mkdir()
        with self.assertRaises(ValueError): prepare(self.base, self.candidate, output)
