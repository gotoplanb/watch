import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("publication", Path(__file__).parents[1] / "check_publication.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name, data=b"Generic source\n"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_private_terms_are_detected_without_recording_values(self):
        self.write("fixture.py", "customer = 'Example Private Identifier'\n".encode())
        result = publication.audit_tree(self.root, ["Example Private Identifier"])
        self.assertEqual(result["findings"][0]["kind"], "private_term")
        self.assertNotIn("Example Private Identifier", json.dumps(result))

    def test_customer_identifiers_in_filenames_are_detected(self):
        self.write("Example-Private-Identifier.py")
        result = publication.audit_tree(self.root, ["Example-Private-Identifier"])
        self.assertEqual(result["findings"][0]["kind"], "private_term_in_path")
        self.assertNotIn("Example-Private-Identifier", json.dumps(result))

    def test_a_failed_initial_review_cannot_be_verified_as_approved(self):
        self.write("fixture.py", b"Example Private Identifier\n")
        result = publication.audit_tree(self.root, ["Example Private Identifier"])
        self.assertFalse(publication.verify_manifest(self.root, result))

    def test_case_unicode_and_invisible_characters_cannot_hide_a_term(self):
        self.write("fixture.py", "ｐｒｉｖａｔｅ\u200bｌａｂｅｌ\n".encode())
        self.assertTrue(publication.audit_tree(self.root, ["PrivateLabel"])["findings"])

    def test_runtime_artifacts_and_sensitive_documents_are_rejected(self):
        for name in (".env", "backend/.env.production", "local/fixtures/raw.json", "local/review/report.txt", "raw.docx", "screenshots/example.png", "mail.eml", ".git/config"):
            self.write(name)
        self.assertEqual(len(publication.audit_tree(self.root)["findings"]), 8)

    def test_symlinks_and_binary_content_are_rejected(self):
        self.write("binary.bin", b"\x00\x01")
        self.write("file.txt")
        (self.root / "link").symlink_to(self.root / "file.txt")
        kinds = {item["kind"] for item in publication.audit_tree(self.root)["findings"]}
        self.assertEqual(kinds, {"symlink", "non_text_artifact"})

    def test_example_environment_and_readme_are_allowed_but_manifest_is_exact(self):
        self.write(".env.example", b"TOKEN=replace-me\n")
        self.write("local/fixtures/README.md")
        report = publication.audit_tree(self.root)
        self.assertFalse(report["findings"])
        self.assertTrue(publication.verify_manifest(self.root, report))
        self.write("extra.txt")
        self.assertFalse(publication.verify_manifest(self.root, report))

    def test_modified_deleted_or_chmod_files_invalidate_the_review(self):
        file = self.write("file.py")
        report = publication.audit_tree(self.root)
        file.chmod(file.stat().st_mode | 0o100)
        self.assertFalse(publication.verify_manifest(self.root, report))
        file.chmod(file.stat().st_mode & ~0o111)
        file.write_text("Changed\n")
        self.assertFalse(publication.verify_manifest(self.root, report))
        file.unlink()
        self.assertFalse(publication.verify_manifest(self.root, report))

    def test_private_machine_paths_and_cloud_accounts_are_rejected(self):
        self.write("config.txt", ("/" + "Users/example-private/source\n" + "arn:aws:iam::" + "123456789012:role/example\n").encode())
        self.assertEqual(len(publication.audit_tree(self.root)["findings"]), 2)

    def test_documented_containers_and_zero_account_emulator_are_not_workstation_identities(self):
        self.write("example.txt", b"/home/node/file.txt\n/home/StepFunctionsLocal/MockConfigFile.json\narn:aws:iam::000000000000:role/Dummy\n")
        self.assertFalse(publication.audit_tree(self.root)["findings"])


if __name__ == "__main__":
    unittest.main()
