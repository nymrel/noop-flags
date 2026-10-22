import importlib.util
import io
import tarfile
import tempfile
import warnings
from zipfile import ZipFile
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

    def test_build_backend_requires_one_exact_hatchling_release(self):
        for requirement in (["hatchling==1.32.3"], ["hatchling==1.33.0"]):
            with self.subTest(requirement=requirement):
                verify_package.verify_build_requirements(requirement)

        for requirement in (
            None,
            [],
            ["hatchling>=1.32.3"],
            ["hatchling==1.32"],
            ["hatchling==1.٣٢.٣"],
            ["hatchling==2.0.0"],
            ["hatchling==1.32.3", "wheel==0.46.1"],
            ["setuptools==80.9.0"],
        ):
            with (
                self.subTest(requirement=requirement),
                self.assertRaisesRegex(
                    verify_package.VerificationError, "single exact hatchling"
                ),
            ):
                verify_package.verify_build_requirements(requirement)

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


class TestDistributionContents(unittest.TestCase):
    VERSION = "0.1.0"
    DIST_INFO = "noop_flags-0.1.0.dist-info"
    SDIST_ROOT = "noop_flags-0.1.0"
    METADATA = (
        b"Metadata-Version: 2.4\nName: noop-flags\nVersion: 0.1.0\n"
        b"Requires-Python: >=3.11\nLicense-Expression: MIT\n\n"
    )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def wheel_entries(self):
        return {
            "noop_flags.py": (ROOT / "noop_flags.py").read_bytes(),
            f"{self.DIST_INFO}/METADATA": self.METADATA,
            f"{self.DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nTag: py3-none-any\n",
            f"{self.DIST_INFO}/RECORD": b"",
            f"{self.DIST_INFO}/entry_points.txt": (
                b"[console_scripts]\nnoop-flags = noop_flags:main\n"
            ),
            f"{self.DIST_INFO}/licenses/LICENSE": (ROOT / "LICENSE").read_bytes(),
        }

    def sdist_entries(self):
        entries = {
            f"{self.SDIST_ROOT}/{name}": (ROOT / name).read_bytes()
            for name in ("noop_flags.py", "pyproject.toml", "LICENSE")
        }
        entries[f"{self.SDIST_ROOT}/PKG-INFO"] = self.METADATA
        return entries

    def write_wheel(self, entries, duplicate=None):
        path = self.directory / "noop_flags-0.1.0-py3-none-any.whl"
        with ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(duplicate, b"substitute payload")
        return path

    def write_sdist(self, entries, duplicate=None):
        path = self.directory / "noop_flags-0.1.0.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if duplicate:
                member = tarfile.TarInfo(duplicate)
                member.size = len(b"substitute payload")
                archive.addfile(member, io.BytesIO(b"substitute payload"))
        return path

    def test_matching_distributions_are_accepted(self):
        for path, verifier in (
            (self.write_wheel(self.wheel_entries()), verify_package.verify_wheel),
            (self.write_sdist(self.sdist_entries()), verify_package.verify_sdist),
        ):
            with self.subTest(path=path.name):
                receipt = verifier(path, self.VERSION)
                self.assertEqual(receipt["path"], path.name)
                self.assertEqual(receipt["sha256"], verify_package.sha256(path))

    def test_wheel_rejects_changed_or_missing_console_entry_point(self):
        for entry_point in (
            None,
            b"[console_scripts]\nnoop-flags = different_module:main\n",
            b"[console_scripts]\nother-command = noop_flags:main\n",
            b"[console_scripts]\nnoop-flags = noop_flags:main\nextra = other:main\n",
            b"not an entry-point file",
        ):
            with self.subTest(entry_point=entry_point):
                entries = self.wheel_entries()
                name = f"{self.DIST_INFO}/entry_points.txt"
                if entry_point is None:
                    del entries[name]
                else:
                    entries[name] = entry_point
                with self.assertRaises(verify_package.VerificationError):
                    verify_package.verify_wheel(self.write_wheel(entries), self.VERSION)

    def test_wheel_rejects_unreviewed_payloads(self):
        for name in (
            "unexpected.pth",
            "unexpected.py",
            "noop_flags-0.1.0.data/purelib/unexpected.pth",
            "extra-0.1.0.dist-info/entry_points.txt",
        ):
            with self.subTest(name=name):
                entries = self.wheel_entries()
                entries[name] = b"import unexpected\n"
                with self.assertRaisesRegex(verify_package.VerificationError, "contents"):
                    verify_package.verify_wheel(self.write_wheel(entries), self.VERSION)

    def test_wheel_rejects_missing_required_distribution_files(self):
        for filename in ("WHEEL", "RECORD"):
            with self.subTest(filename=filename):
                entries = self.wheel_entries()
                del entries[f"{self.DIST_INFO}/{filename}"]
                with self.assertRaisesRegex(verify_package.VerificationError, "contents"):
                    verify_package.verify_wheel(self.write_wheel(entries), self.VERSION)

    def test_wheel_rejects_payload_substitution_even_with_same_version(self):
        entries = self.wheel_entries()
        entries["noop_flags.py"] += b"\n# altered after review\n"
        with self.assertRaisesRegex(verify_package.VerificationError, "source checkout"):
            verify_package.verify_wheel(self.write_wheel(entries), self.VERSION)

    def test_sdist_rejects_version_metadata_and_payload_substitution(self):
        for name, data in (
            ("PKG-INFO", self.METADATA.replace(b"Version: 0.1.0", b"Version: 9.9.9")),
            ("noop_flags.py", (ROOT / "noop_flags.py").read_bytes() + b"\n# altered\n"),
            ("pyproject.toml", (ROOT / "pyproject.toml").read_bytes().replace(
                b'version = "0.1.0"', b'version = "9.9.9"'
            )),
        ):
            with self.subTest(name=name):
                entries = self.sdist_entries()
                entries[f"{self.SDIST_ROOT}/{name}"] = data
                with self.assertRaises(verify_package.VerificationError):
                    verify_package.verify_sdist(self.write_sdist(entries), self.VERSION)

    def test_duplicate_archive_members_are_rejected(self):
        for name in ("noop_flags.py", "./noop_flags.py"):
            with self.subTest(kind="wheel", name=name):
                path = self.write_wheel(self.wheel_entries(), duplicate=name)
                with self.assertRaisesRegex(verify_package.VerificationError, "duplicate"):
                    verify_package.verify_wheel(path, self.VERSION)
        for name in (f"{self.SDIST_ROOT}/noop_flags.py", f"{self.SDIST_ROOT}/./noop_flags.py"):
            with self.subTest(kind="sdist", name=name):
                path = self.write_sdist(self.sdist_entries(), duplicate=name)
                with self.assertRaisesRegex(verify_package.VerificationError, "duplicate"):
                    verify_package.verify_sdist(path, self.VERSION)


if __name__ == "__main__":
    unittest.main()
