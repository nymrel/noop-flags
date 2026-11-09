"""Opt-in checked-scan gate and unchanged legacy behavior."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import noop_flags

IGNORED = (
    "import argparse\n"
    "ap = argparse.ArgumentParser()\n"
    'ap.add_argument("--ignored")\n'
    "args = ap.parse_args()\n"
)
CLEAN = IGNORED + "print(args.ignored)\n"


class TestCompleteScan(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_checked(self, target=None, output="json"):
        args = [str(target or self.root), "--require-complete"]
        if output != "plain":
            args.append("--" + output)
        stream, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(stream), redirect_stderr(errors):
            code = noop_flags.main(args)
        self.assertEqual(errors.getvalue(), "")
        text = stream.getvalue()
        report = text if output == "plain" else json.loads(text)
        coverage = None if output == "plain" else report["coverage"] if output == "json" else report["runs"][0]["properties"]["completeScan"]
        return code, report, coverage

    def source_metadata(self, source):
        with source.open("rb") as stream:
            by_path = source.lstat()
            by_fd = os.fstat(stream.fileno())
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns",
                  "st_ctime_ns", "st_birthtime_ns")
        return {"path": {key: getattr(by_path, key, None) for key in fields},
                "descriptor": {key: getattr(by_fd, key, None) for key in fields}}

    def test_complete_clean_and_finding_scans_have_exact_exit_precedence(self):
        source = self.write("space café.py", CLEAN)
        for mode in ("json", "sarif", "plain"):
            with self.subTest(mode=mode):
                code, report, coverage = self.run_checked(output=mode)
                self.assertEqual(code, 0)
                if coverage:
                    self.assertTrue(coverage["complete"])
                    self.assertTrue(coverage["discoveryComplete"])
                    self.assertEqual(coverage["schema"], "noop-flags.complete-scan.v1")
                    self.assertEqual(coverage["exitCode"], code)
                    self.assertEqual((coverage["candidateFiles"], coverage["analyzedFiles"]), (1, 1))
                else:
                    self.assertIn("gate: PASS", report)
        source.write_text(IGNORED, encoding="utf-8")
        for mode in ("json", "sarif", "plain"):
            with self.subTest(mode=mode):
                code, report, coverage = self.run_checked(output=mode)
                self.assertEqual(code, 1, json.dumps({"report": report,
                                 "metadata": self.source_metadata(source)}))
                if coverage:
                    self.assertTrue(coverage["complete"])
                    self.assertEqual(coverage["exitCode"], 1)
                else:
                    self.assertIn("gate: FINDINGS", report)

    def test_incomplete_overrides_findings_and_retains_them(self):
        self.write("finding.py", IGNORED)
        self.write("dynamic.py", IGNORED + "print(vars(args))\n")
        for mode in ("json", "sarif", "plain"):
            with self.subTest(mode=mode):
                code, report, coverage = self.run_checked(output=mode)
                self.assertEqual(code, 2)
                if coverage:
                    self.assertFalse(coverage["complete"])
                    self.assertEqual(coverage["analyzedFiles"], 1)
                    self.assertEqual(coverage["skippedFiles"], 1)
                    results = report["findings"] if mode == "json" else report["runs"][0]["results"]
                    self.assertEqual(len(results), 1)
                else:
                    self.assertIn("gate: INCOMPLETE", report)
                    self.assertIn("--ignored", report)

    def test_rewritten_source_is_stable_before_a_new_scan(self):
        source = self.write("rewritten.py", CLEAN)
        self.assertEqual(self.run_checked()[0], 0)
        source.write_text(IGNORED, encoding="utf-8")
        code, report, _ = self.run_checked()
        self.assertEqual(code, 1, json.dumps({"report": report,
                         "metadata": self.source_metadata(source)}))

    def test_zero_sources_is_incomplete_even_with_successful_traversal(self):
        self.write("notes.txt", "outside directory source selection")
        code, _, coverage = self.run_checked()
        self.assertEqual(code, 2)
        self.assertFalse(coverage["complete"])
        self.assertTrue(coverage["discoveryComplete"])
        self.assertEqual(coverage["candidateFiles"], 0)

    def test_syntax_failure_has_visible_source_skip(self):
        self.write("broken.py", "def broken(:\n")
        code, report, coverage = self.run_checked()
        self.assertEqual(code, 2)
        self.assertEqual(coverage["skippedFiles"], 1)
        self.assertTrue(report["skipped"][0]["reason"].startswith("syntax error:"))

    def test_python_encodings_are_strict_and_bom_is_supported(self):
        path = self.root / "source.py"
        for raw in (b"\xef\xbb\xbf" + CLEAN.encode(),
                    b"# coding: latin-1\n# caf\xe9\n" + CLEAN.encode()):
            with self.subTest(raw=raw[:25]):
                path.write_bytes(raw)
                self.assertEqual(self.run_checked()[0], 0)
        for raw in (b"# invalid \xff\n" + CLEAN.encode(),
                    b"# coding: not-a-codec\n" + CLEAN.encode()):
            with self.subTest(raw=raw[:25]):
                path.write_bytes(raw)
                code, report, coverage = self.run_checked()
                self.assertEqual(code, 2)
                self.assertEqual(coverage["analyzedFiles"], 0)
                self.assertTrue(report["skipped"][0]["reason"].startswith("unreadable:"))

    def test_observed_file_mutation_discards_stale_findings(self):
        target = self.write("source.py", IGNORED)
        original = noop_flags.analyse_source

        def changing(source, label="<src>"):
            result = original(source, label)
            target.write_text(CLEAN + "# changed length\n", encoding="utf-8")
            return result

        with patch.object(noop_flags, "analyse_source", changing):
            code, report, coverage = self.run_checked()
        self.assertEqual(code, 2)
        self.assertEqual(report["findings"], [])
        self.assertEqual(coverage["analyzedFiles"], 0)
        self.assertTrue(report["skipped"][0]["reason"].startswith("source changed:"))

    def test_observed_directory_mutation_is_not_certified(self):
        self.write("source.py", CLEAN)
        original = noop_flags.analyse_source

        def adding(source, label="<src>"):
            result = original(source, label)
            self.write("added.py", IGNORED)
            return result

        with patch.object(noop_flags, "analyse_source", adding):
            code, _, coverage = self.run_checked()
        self.assertEqual(code, 2)
        self.assertFalse(coverage["discoveryComplete"])
        self.assertIn("directory-changed", {row["category"] for row in coverage["discoveryDiagnostics"]})

    def test_named_exclusions_are_pruned_before_walk(self):
        self.write("clean.py", CLEAN)
        self.write(".venv/ignored.py", IGNORED)
        self.write("node_modules/ignored.py", IGNORED)
        code, _, coverage = self.run_checked()
        self.assertEqual(code, 0)
        self.assertEqual(coverage["candidateFiles"], 1)

    def test_missing_or_excluded_target_is_incomplete_in_all_formats(self):
        excluded = self.write(".venv/source.py", CLEAN)
        for path in (self.root / "absent", excluded):
            for mode in ("plain", "json", "sarif"):
                with self.subTest(path=path, mode=mode):
                    self.assertEqual(self.run_checked(path, mode)[0], 2)

    def make_link(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation unavailable: {type(error).__name__}")

    def test_root_file_directory_and_ancestor_links_are_refused_before_resolution(self):
        source = self.write("real/source.py", CLEAN)
        file_link = self.root / "file-link.py"
        dir_link = self.root / "dir-link"
        self.make_link(source, file_link)
        self.make_link(source.parent, dir_link, True)
        for path in (file_link, dir_link, dir_link / "source.py"):
            with self.subTest(path=path):
                code, _, coverage = self.run_checked(path)
                self.assertEqual(code, 2)
                self.assertFalse(coverage["discoveryComplete"])
                self.assertEqual(coverage["discoveryDiagnostics"][0]["category"], "symlink-target")

    def test_internal_links_do_not_run_outside_the_selected_tree(self):
        source = self.write("real/source.py", CLEAN)
        self.make_link(source, self.root / "linked.py")
        self.make_link(source.parent, self.root / "linked-dir", True)
        code, report, coverage = self.run_checked()
        self.assertEqual(code, 2)
        self.assertEqual(coverage["candidateFiles"], 2)
        self.assertEqual(coverage["analyzedFiles"], 1)
        self.assertEqual(len(report["skipped"]), 1)
        self.assertEqual(len(coverage["discoveryDiagnostics"]), 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_actual_unreadable_source_and_subtree_fail_the_gate(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root bypasses POSIX permission denial")
        self.write("clean.py", CLEAN)
        blocked_file = self.write("unreadable.py", IGNORED)
        blocked_dir = self.root / "hidden"
        self.write("hidden/finding.py", IGNORED)
        blocked_file.chmod(0)
        blocked_dir.chmod(0)
        try:
            code, report, coverage = self.run_checked()
            self.assertEqual(code, 2)
            self.assertFalse(coverage["discoveryComplete"])
            self.assertEqual(coverage["candidateFiles"], 2)
            self.assertEqual(coverage["analyzedFiles"], 1)
            self.assertEqual(len(report["skipped"]), 1)
            self.assertIn("traversal-error", {row["category"] for row in coverage["discoveryDiagnostics"]})
        finally:
            blocked_file.chmod(0o600)
            blocked_dir.chmod(0o700)

    @unittest.skipIf(os.name == "nt" or not hasattr(os, "mkfifo"), "POSIX FIFO")
    def test_special_file_is_refused_without_opening_or_blocking(self):
        self.write("clean.py", CLEAN)
        os.mkfifo(self.root / "pipe.py")
        result = subprocess.run([sys.executable, str(Path(noop_flags.__file__).resolve()),
                                 str(self.root), "--require-complete", "--json"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertTrue(report["skipped"][0]["reason"].startswith("unsupported source:"))
        self.assertEqual(report["coverage"]["candidateFiles"], 2)

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_windows_junction_target_is_refused(self):
        self.write("real/source.py", CLEAN)
        junction = self.root / "junction"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.root / "real")],
                                capture_output=True, text=True)
        if result.returncode:
            self.skipTest("junction creation unavailable")
        try:
            self.assertEqual(self.run_checked(junction)[0], 2)
            self.assertEqual(self.run_checked(junction / "source.py")[0], 2)
        finally:
            junction.rmdir()

    def test_analyzer_failure_is_incomplete_and_process_control_still_propagates(self):
        self.write("source.py", CLEAN)
        with patch.object(noop_flags, "analyse_source", side_effect=RuntimeError("PRIVATE_DETAIL")):
            code, report, _ = self.run_checked()
        self.assertEqual(code, 2)
        self.assertNotIn("PRIVATE_DETAIL", json.dumps(report))
        with patch.object(noop_flags, "analyse_source", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_checked()

    def test_gate_rejects_selftest_combination(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            noop_flags.main(["--selftest", "--require-complete"])
        self.assertEqual(error.exception.code, 2)

    def test_default_modes_do_not_gain_checked_coverage(self):
        self.write("source.py", CLEAN)
        for flag in ("--json", "--sarif"):
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(noop_flags.main([str(self.root), flag]), 0)
            report = json.loads(stream.getvalue())
            self.assertNotIn("coverage", report)
            if flag == "--sarif":
                run = report["runs"][0]
                self.assertNotIn("completeScan", run["properties"])
                self.assertNotIn("exitCode", run["invocations"][0])
                self.assertEqual(run["properties"]["discoveryCoverage"], "unknown")


if __name__ == "__main__":
    unittest.main()
