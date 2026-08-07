"""Tests for noop-flags.

Run with either:
    python -m unittest -v          (no dependencies)
    pytest                          (if you prefer)

Every test here asserts in BOTH directions where it can: that the tool reports
the flag when it is genuinely ignored, and that it stays quiet when the same
flag is read. A guard proven only in the failing direction may be refusing
everything.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import noop_flags

HERE = Path(__file__).resolve().parent


def dests(src: str) -> list[str]:
    found, _ = noop_flags.analyse_source(textwrap.dedent(src))
    return sorted(f["dest"] for f in found)


def skip_reason(src: str) -> str | None:
    _, reason = noop_flags.analyse_source(textwrap.dedent(src))
    return reason


class TestDetection(unittest.TestCase):
    def test_ignored_flag_is_reported(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            print("hello")
        '''), ["product"])

    def test_the_same_flag_is_silent_once_it_is_read(self):
        """The other direction. Without this, a tool that reports everything
        would still pass the test above."""
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            print(args.product)
        '''), [])

    def test_read_inside_a_nested_function_still_counts(self):
        self.assertEqual(dests('''
            import argparse
            def main():
                ap = argparse.ArgumentParser()
                ap.add_argument("--depth")
                args = ap.parse_args()
                def inner():
                    return args.depth
                return inner()
        '''), [])

    def test_dashes_become_underscores(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--dry-run", action="store_true")
            args = ap.parse_args()
        '''), ["dry_run"])

    def test_explicit_dest_wins_over_flag_name(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--thing", dest="renamed")
            args = ap.parse_args()
            print(args.renamed)
        '''), [])

    def test_short_only_flag_uses_the_short_name(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("-v")
            args = ap.parse_args()
        '''), ["v"])

    def test_long_option_wins_when_both_are_given(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("-p", "--product")
            args = ap.parse_args()
        '''), ["product"])

    def test_positional_argument(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("path")
            args = ap.parse_args()
        '''), ["path"])

    def test_version_action_is_argparse_own_business(self):
        """Regression: found by running this tool against its own source."""
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--version", action="version", version="1.0")
            ap.add_argument("--real")
            args = ap.parse_args()
        '''), ["real"])

    def test_help_action_is_argparse_own_business(self):
        self.assertEqual(dests('''
            import argparse
            ap = argparse.ArgumentParser(add_help=False)
            ap.add_argument("-h", "--help", action="help")
            args = ap.parse_args()
        '''), [])


class TestRefusals(unittest.TestCase):
    """The three shapes that make a static read-check unsound. Each of these
    produced a false positive on real code before it was handled."""

    def test_dynamic_getattr_refuses(self):
        self.assertEqual(skip_reason('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            field = "product"
            print(getattr(args, field))
        '''), "dynamic getattr/setattr")

    def test_namespace_passed_to_a_call_refuses(self):
        self.assertEqual(skip_reason('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            helper(args)
        '''), "namespace passed to another call")

    def test_vars_refuses(self):
        self.assertEqual(skip_reason('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            print(vars(args))
        '''), "vars(namespace)")

    def test_dunder_dict_refuses(self):
        self.assertEqual(skip_reason('''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            args = ap.parse_args()
            print(args.__dict__)
        '''), "namespace.__dict__")

    def test_returned_namespace_refuses(self):
        self.assertEqual(skip_reason('''
            import argparse
            def parse():
                ap = argparse.ArgumentParser()
                ap.add_argument("--product")
                args = ap.parse_args()
                return args
        '''), "namespace returned")

    def test_getattr_with_a_literal_is_a_read_not_an_escape(self):
        """The exemption that keeps the escape rule from swallowing whole
        files. Without it, one literal getattr hides every ignored flag."""
        src = '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--product")
            ap.add_argument("--other")
            args = ap.parse_args()
            print(getattr(args, "product"))
        '''
        self.assertIsNone(skip_reason(src))
        self.assertEqual(dests(src), ["other"])

    def test_unparseable_file_is_skipped_not_passed(self):
        reason = skip_reason("def (:")
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("syntax error"))

    def test_a_skipped_file_yields_no_findings(self):
        """UNKNOWN is not a pass, and it is not a finding either."""
        found, reason = noop_flags.analyse_source(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            'ap.add_argument("--x")\n'
            "args = ap.parse_args()\n"
            "print(vars(args))\n")
        self.assertEqual(found, [])
        self.assertIsNotNone(reason)


class TestScan(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, body: str) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return p

    def test_scan_finds_across_files_and_counts_skips_separately(self):
        self.write("bad.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--ignored")
            args = ap.parse_args()
        ''')
        self.write("unprovable.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--whatever")
            args = ap.parse_args()
            print(vars(args))
        ''')
        self.write("clean.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--used")
            args = ap.parse_args()
            print(args.used)
        ''')
        findings, skipped = noop_flags.scan(self.root)
        self.assertEqual([f["dest"] for f in findings], ["ignored"])
        self.assertEqual([s["file"] for s in skipped], ["unprovable.py"])

    def test_scan_ignores_vendored_and_cache_directories(self):
        self.write("node_modules/pkg/x.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--vendored")
            args = ap.parse_args()
        ''')
        self.write(".venv/lib/y.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--alsovendored")
            args = ap.parse_args()
        ''')
        findings, _ = noop_flags.scan(self.root)
        self.assertEqual(findings, [])

    def test_scan_accepts_a_single_file(self):
        p = self.write("solo.py", '''
            import argparse
            ap = argparse.ArgumentParser()
            ap.add_argument("--solo")
            args = ap.parse_args()
        ''')
        findings, _ = noop_flags.scan(p)
        self.assertEqual([f["dest"] for f in findings], ["solo"])


class TestExitCodes(unittest.TestCase):
    """The exit code is the contract CI depends on, so assert it directly."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = noop_flags.main(list(argv))
        return code, buf.getvalue()

    def test_exit_1_when_a_flag_is_ignored(self):
        (self.root / "a.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            'ap.add_argument("--ignored")\n'
            "args = ap.parse_args()\n", encoding="utf-8")
        code, out = self.run_main(str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("--ignored", out)

    def test_exit_0_when_clean(self):
        (self.root / "a.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            'ap.add_argument("--used")\n'
            "args = ap.parse_args()\n"
            "print(args.used)\n", encoding="utf-8")
        code, _ = self.run_main(str(self.root))
        self.assertEqual(code, 0)

    def test_exit_2_when_the_path_does_not_exist(self):
        code = noop_flags.main([str(self.root / "nope")])
        self.assertEqual(code, 2)

    def test_json_output_is_valid_and_carries_both_lists(self):
        (self.root / "a.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            'ap.add_argument("--ignored")\n'
            "args = ap.parse_args()\n", encoding="utf-8")
        code, out = self.run_main(str(self.root), "--json")
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["findings"][0]["flag"], "--ignored")
        self.assertIn("skipped", payload)

    def test_skipped_files_are_announced_even_without_the_flag(self):
        (self.root / "a.py").write_text(
            "import argparse\n"
            "ap = argparse.ArgumentParser()\n"
            'ap.add_argument("--x")\n'
            "args = ap.parse_args()\n"
            "print(vars(args))\n", encoding="utf-8")
        code, out = self.run_main(str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("SKIPPED", out)


class TestDogfood(unittest.TestCase):
    def test_selftest_subcommand_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = noop_flags.main(["--selftest"])
        self.assertEqual(code, 0, buf.getvalue())

    def test_the_tool_is_clean_against_its_own_source(self):
        """It flagged its own --version on the first run. It should not now."""
        findings, skipped = noop_flags.scan(HERE / "noop_flags.py")
        self.assertEqual(findings, [], f"noop-flags flags itself: {findings}")
        self.assertEqual(skipped, [])

    def test_it_runs_as_a_script_end_to_end(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "noop_flags.py"), "--selftest"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("proved", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
