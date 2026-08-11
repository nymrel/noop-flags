from pathlib import Path
import tempfile
import unittest

from release_proof import safe_relative_member, sha256


class TestReleaseProofHelpers(unittest.TestCase):
    def test_archive_member_must_stay_under_expected_root(self):
        self.assertEqual(
            safe_relative_member("noop_flags-0.1.0/README.md", "noop_flags-0.1.0"),
            "README.md",
        )
        with self.assertRaises(RuntimeError):
            safe_relative_member("noop_flags-0.1.0/../secret", "noop_flags-0.1.0")
        with self.assertRaises(RuntimeError):
            safe_relative_member("other/README.md", "noop_flags-0.1.0")

    def test_sensitive_archive_names_are_rejected(self):
        with self.assertRaises(RuntimeError):
            safe_relative_member("noop_flags-0.1.0/.env", "noop_flags-0.1.0")

    def test_sha256_is_deterministic(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "sample"
            path.write_bytes(b"noop-flags")
            self.assertEqual(
                sha256(path),
                "aaaf54d85997bcda53a4a7b637d340cc261688b6bd6d26204d00ffb402c4e86b",
            )


if __name__ == "__main__":
    unittest.main()
