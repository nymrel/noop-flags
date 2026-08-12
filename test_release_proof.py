import tarfile
from pathlib import Path
import tempfile
import unittest
import zipfile

from release_proof import inspect_sdist, inspect_wheel, safe_relative_member, sha256


class TestReleaseProofHelpers(unittest.TestCase):
    WHEEL_NAME = "noop_flags-0.1.0-py3-none-any.whl"
    ALLOWED_WHEEL_MEMBERS = {
        "noop_flags.py",
        "noop_flags-0.1.0.dist-info/METADATA",
        "noop_flags-0.1.0.dist-info/WHEEL",
        "noop_flags-0.1.0.dist-info/RECORD",
        "noop_flags-0.1.0.dist-info/entry_points.txt",
        "noop_flags-0.1.0.dist-info/licenses/LICENSE",
    }

    def write_wheel(self, path, members):
        with zipfile.ZipFile(path, "w") as archive:
            for member in members:
                archive.writestr(member, b"test")

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

    def test_wheel_rejects_data_install_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / self.WHEEL_NAME
            members = self.ALLOWED_WHEEL_MEMBERS | {
                "noop_flags-0.1.0.data/purelib/evil.py"
            }
            self.write_wheel(wheel, members)
            with self.assertRaisesRegex(RuntimeError, r"unexpected=.*evil\.py"):
                inspect_wheel(wheel)

    def test_wheel_with_only_allowed_members_passes(self):
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / self.WHEEL_NAME
            self.write_wheel(wheel, self.ALLOWED_WHEEL_MEMBERS)
            self.assertEqual(
                inspect_wheel(wheel),
                sorted(self.ALLOWED_WHEEL_MEMBERS),
            )

    def test_sdist_rejects_symlink_member(self):
        with tempfile.TemporaryDirectory() as raw:
            sdist = Path(raw) / "noop_flags-0.1.0.tar.gz"
            with tarfile.open(sdist, "w:gz") as archive:
                member = tarfile.TarInfo("noop_flags-0.1.0/link")
                member.type = tarfile.SYMTYPE
                member.linkname = "noop_flags.py"
                archive.addfile(member)
            with self.assertRaisesRegex(RuntimeError, r"link \(type=symlink\)"):
                inspect_sdist(sdist)


if __name__ == "__main__":
    unittest.main()
