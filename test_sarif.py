"""SARIF integration tests; scanner behavior remains the existing contract."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote_to_bytes

import noop_flags


IGNORED = (
    "import argparse\n"
    "ap = argparse.ArgumentParser()\n"
    'ap.add_argument("--ignored")\n'
    "args = ap.parse_args()\n"
)


class TestSarif(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Match the CLI's canonical root, including Windows short-name aliases.
        self.root = Path(self.temporary.name).resolve()

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return path

    def run_sarif(self, target=None):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = noop_flags.main(["--sarif", str(target or self.root)])
        self.assertEqual(errors.getvalue(), "")
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_finding_location_matches_json_and_preserves_exit_code(self):
        self.write("cli.py", IGNORED)
        findings, skipped = noop_flags.scan(self.root)
        code, report, text = self.run_sarif()
        self.assertEqual(code, 1)
        self.assertEqual(report["version"], "2.1.0")
        run = report["runs"][0]
        self.assertEqual(run["tool"]["driver"]["semanticVersion"], noop_flags.__version__)
        self.assertEqual(len(run["results"]), len(findings))
        self.assertEqual(skipped, [])
        result = run["results"][0]
        self.assertEqual(result["ruleId"], "NF001")
        self.assertEqual(result["ruleIndex"], 0)
        location = result["locations"][0]["physicalLocation"]
        self.assertEqual(location["artifactLocation"]["uri"], "cli.py")
        self.assertEqual(location["region"], {"startLine": 3})
        self.assertIn("--ignored", result["message"]["text"])
        self.assertTrue(run["invocations"][0]["executionSuccessful"])
        self.assertEqual(run["properties"]["analysisCoverage"], "complete")
        self.assertEqual(run["properties"]["discoveryCoverage"], "unknown")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("snippet", text)

    def test_a_read_argument_stays_clean_in_both_formats(self):
        self.write("cli.py", IGNORED + "print(args.ignored)\n")
        code, report, _ = self.run_sarif()
        self.assertEqual(code, 0)
        self.assertEqual(report["runs"][0]["results"], [])
        self.assertEqual(noop_flags.scan(self.root), ([], []))

    def test_relative_uri_encodes_spaces_unicode_and_reserved_characters(self):
        name = "nested space/café #100%?.py"
        if os.name == "nt":
            name = "nested space/café #100%.py"
        self.write(name, IGNORED)
        code, report, _ = self.run_sarif()
        self.assertEqual(code, 1)
        uri = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(os.fsdecode(unquote_to_bytes(uri)), name)
        self.assertNotIn(" ", uri)
        self.assertIn("%C3%A9", uri)
        self.assertIn("%23", uri)
        self.assertIn("%25", uri)

    @unittest.skipIf(os.name == "nt", "backslash is a separator on Windows")
    def test_posix_literal_backslash_is_not_a_directory_separator(self):
        name = "literal\\name.py"
        self.write(name, IGNORED)
        _, report, _ = self.run_sarif()
        uri = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(uri, "literal%5Cname.py")

    @unittest.skipIf(os.name == "nt", "POSIX undecodable-byte filename")
    def test_filesystem_byte_names_remain_percent_encoded(self):
        name = os.fsdecode(b"bad-\xff.py")
        self.write(name, IGNORED)
        code, report, _ = self.run_sarif()
        self.assertEqual(code, 1)
        uri = report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(unquote_to_bytes(uri), b"bad-\xff.py")

    def test_one_file_scan_uses_its_filename(self):
        path = self.write("cli.py", IGNORED)
        _, report, _ = self.run_sarif(path)
        self.assertEqual(report["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "cli.py")
        self.assertEqual(report["runs"][0]["properties"]["discoveryCoverage"], "complete")

    def test_mixed_skips_are_visible_and_never_counted_as_analyzed(self):
        self.write("finding.py", IGNORED)
        self.write("clean.py", "print('SOURCE_SECRET')\n")
        self.write("dynamic.py", IGNORED + "print(vars(args))\n")
        self.write("syntax.py", "def broken(:\n")
        code, report, text = self.run_sarif()
        self.assertEqual(code, 1)
        run = report["runs"][0]
        self.assertEqual({key: run["properties"][key] for key in
                          ("candidateFiles", "analyzedFiles", "skippedFiles", "analysisCoverage")},
                         {"candidateFiles": 4, "analyzedFiles": 2,
                          "skippedFiles": 2, "analysisCoverage": "partial"})
        self.assertFalse(run["invocations"][0]["executionSuccessful"])
        notes = run["invocations"][0]["toolExecutionNotifications"]
        self.assertEqual({row["properties"]["category"] for row in notes if "properties" in row},
                         {"unprovable", "syntax-error"})
        self.assertNotIn("SOURCE_SECRET", text)

    def test_all_unprovable_is_not_a_clean_scan_or_execution_crash(self):
        self.write("dynamic.py", IGNORED + "print(vars(args))\n")
        code, report, _ = self.run_sarif()
        self.assertEqual(code, 0)  # Existing no-findings exit policy.
        run = report["runs"][0]
        self.assertEqual(run["results"], [])
        self.assertEqual(run["properties"]["analysisCoverage"], "none")
        self.assertEqual(run["properties"]["analyzedFiles"], 0)
        self.assertTrue(run["invocations"][0]["executionSuccessful"])
        self.assertEqual(len(run["invocations"][0]["toolExecutionNotifications"]), 3)

    def test_directory_discovery_never_certifies_complete_enumeration(self):
        self.write("clean.py", "print('ok')\n")
        _, report, _ = self.run_sarif()
        run = report["runs"][0]
        self.assertEqual(run["properties"]["analysisCoverage"], "complete")
        self.assertEqual(run["properties"]["discoveryCoverage"], "unknown")
        self.assertIn("NF-DISCOVERY-UNKNOWN", [row["descriptor"]["id"] for row in
                      run["invocations"][0]["toolExecutionNotifications"]])

    def test_no_sources_is_explicit_even_though_exit_code_stays_zero(self):
        self.write("notes.txt", "not source")
        code, report, _ = self.run_sarif()
        self.assertEqual(code, 0)
        run = report["runs"][0]
        self.assertEqual(run["properties"]["candidateFiles"], 0)
        self.assertEqual(run["properties"]["analysisCoverage"], "none")
        self.assertEqual(run["invocations"][0]["toolExecutionNotifications"][0]["descriptor"]["id"], "NF-NO-ANALYSIS")

    def test_excluded_directories_do_not_inflate_coverage(self):
        self.write(".venv/ignored.py", IGNORED)
        self.write("clean.py", "print('ok')\n")
        _, report, _ = self.run_sarif()
        self.assertEqual(report["runs"][0]["properties"]["candidateFiles"], 1)

    def test_missing_path_emits_sarif_without_its_absolute_path(self):
        missing = self.root / "PRIVATE_MISSING"
        code, report, text = self.run_sarif(missing)
        self.assertEqual(code, 2)
        run = report["runs"][0]
        self.assertEqual(run["properties"]["analysisCoverage"], "unavailable")
        self.assertFalse(run["invocations"][0]["executionSuccessful"])
        self.assertNotIn("PRIVATE_MISSING", text)
        self.assertNotIn(str(self.root), text)

    def test_read_failure_uses_coarse_message_and_relative_location(self):
        blocked = self.write("blocked.py", IGNORED)
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == blocked:
                raise OSError("PRIVATE_EXCEPTION_PATH_AND_TEXT")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_text):
            code, report, text = self.run_sarif()
        self.assertEqual(code, 0)
        run = report["runs"][0]
        self.assertFalse(run["invocations"][0]["executionSuccessful"])
        note = run["invocations"][0]["toolExecutionNotifications"][0]
        self.assertEqual(note["properties"]["category"], "unreadable")
        self.assertEqual(note["locations"][0]["physicalLocation"]["artifactLocation"]["uri"], "blocked.py")
        self.assertNotIn("PRIVATE_EXCEPTION", text)

    def test_path_resolution_and_traversal_failures_stay_structured(self):
        for method in ("resolve", "rglob"):
            with self.subTest(method=method):
                with patch.object(Path, method, side_effect=OSError("PRIVATE_EXCEPTION")):
                    code, report, text = self.run_sarif()
                self.assertEqual(code, 2)
                self.assertEqual(report["runs"][0]["properties"]["analysisCoverage"], "unavailable")
                self.assertNotIn("PRIVATE_EXCEPTION", text)

    def test_payload_is_deterministic_and_plain_json_behavior_is_unchanged(self):
        self.write("cli.py", IGNORED)
        _, first, text = self.run_sarif()
        self.assertEqual(self.run_sarif()[2], text)
        output = io.StringIO()
        with redirect_stdout(output):
            code = noop_flags.main([str(self.root), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(set(json.loads(output.getvalue())), {"root", "findings", "skipped"})
        self.assertEqual(len(first["runs"][0]["results"]), 1)

    def test_conflicting_formats_and_sarif_selftest_are_usage_errors(self):
        for args in (["--json", "--sarif"], ["--selftest", "--sarif"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    noop_flags.main(args)
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
