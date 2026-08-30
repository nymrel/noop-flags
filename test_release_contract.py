import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "verify_package",
    ROOT / "scripts" / "verify_package.py",
)
assert SPEC is not None and SPEC.loader is not None
verify_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_package)


class TestReleaseContract(unittest.TestCase):
    def test_current_source_contract(self):
        self.assertEqual(verify_package.verify_source(ROOT), "0.1.0")

    def test_exact_version_tag_is_required(self):
        verify_package.verify_tag("v0.1.0", "0.1.0")
        with self.assertRaisesRegex(verify_package.VerificationError, "does not match"):
            verify_package.verify_tag("v0.1.1", "0.1.0")

    def test_archive_paths_fail_closed(self):
        for unsafe_path in (
            "../outside",
            "pkg/../../outside",
            "/absolute",
            "..\\outside",
        ):
            with (
                self.subTest(path=unsafe_path),
                self.assertRaises(verify_package.VerificationError),
            ):
                verify_package.verify_archive_path(unsafe_path)

    def test_normal_archive_paths_are_allowed(self):
        verify_package.verify_archive_path("noop_flags-0.1.0/noop_flags.py")


if __name__ == "__main__":
    unittest.main()
