#!/usr/bin/env python
"""noop-flags -- find the CLI flags your program accepts and never reads.

A flag the caller believes selects behaviour, which in fact selects nothing, is
a check that passes without exercising anything. The operator reads a green that
was never earned.

HOW IT DECIDES. For each argparse `add_argument`, derive the destination name
the way argparse itself does, then look for reads of that attribute on the
namespace returned by `parse_args()` in the same file. Declared, never read ->
reported.

WHAT IT REFUSES TO GUESS. Three shapes make a static read-check unsound. When a
file contains one, the whole file is SKIPPED and counted, never silently passed:

  1. getattr(args, name) where `name` is not a literal string
  2. the namespace escaping into another call, or being returned
  3. vars(args) / args.__dict__

Each of those was added because this tool reported a false positive on real
code. Unfiltered, the first run over one repository reported 75 candidates; 57
were one of the three shapes above. Precision matters more than recall here: a
sweep that cries wolf gets ignored, and then the real bug ships.

Usage:
  python noop_flags.py                 # scan the current directory
  python noop_flags.py path/to/src
  python noop_flags.py --json
  python noop_flags.py --show-skipped
  python noop_flags.py --selftest

Exit codes:
  0  no ignored parameters found
  1  at least one flag is accepted and never read
  2  the scan could not run (bad path)
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote_from_bytes

__version__ = "0.1.0"


# Actions argparse consumes itself during parsing. Their dest exists on the
# namespace but is never meant to be read, so reporting them is a false
# positive. Found by running this tool against its own source -- it flagged its
# own --version. When a class is real it shows up inside the machinery built to
# detect it, so sweep your own instrument first.
SELF_CONSUMED_ACTIONS = {"version", "help"}


def dest_for(call: ast.Call) -> tuple[str | None, str | None]:
    """Mirror argparse's own dest derivation: explicit dest=, else the first
    long option with dashes turned into underscores, else the positional.
    Returns (None, None) for actions argparse consumes itself."""
    explicit = None
    for kw in call.keywords:
        if kw.arg == "action" and isinstance(kw.value, ast.Constant) \
                and kw.value.value in SELF_CONSUMED_ACTIONS:
            return None, None
        if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
            explicit = kw.value.value
    opts = [a.value for a in call.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)]
    if not opts:
        return None, None
    flag = opts[0]
    if explicit:
        return explicit, flag
    longs = [o for o in opts if o.startswith("--")]
    if longs:
        return longs[0][2:].replace("-", "_"), longs[0]
    return flag.lstrip("-").replace("-", "_"), flag


def _namespace_names(tree: ast.AST) -> set[str]:
    """Names bound to the result of parse_args(). Falls back to the common
    conventions when the call is not a normal or annotated assignment."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and isinstance(n.value, ast.Call):
            f = n.value.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "parse_args":
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names or {"args", "a", "ns", "opts"}


def unprovable(tree: ast.AST) -> str | None:
    """Return the reason this file's reads cannot be proven statically, or None."""
    ns = _namespace_names(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            namespace_arg = bool(
                n.args and isinstance(n.args[0], ast.Name) and n.args[0].id in ns
            )
            if n.func.id == "vars" and namespace_arg:
                return "vars(namespace)"
            if n.func.id in ("getattr", "hasattr") and len(n.args) >= 2:
                second = n.args[1]
                if namespace_arg and not (isinstance(second, ast.Constant)
                        and isinstance(second.value, str)):
                    return "dynamic getattr/hasattr"
        if (isinstance(n, ast.Attribute) and n.attr == "__dict__"
                and isinstance(n.value, ast.Name) and n.value.id in ns):
            return "namespace.__dict__"

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            # getattr(args, "literal") and hasattr(args, "literal") name exactly
            # one attribute, so they are precise reads rather than escapes.
            if isinstance(n.func, ast.Name) and n.func.id in ("getattr", "hasattr") \
                    and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) \
                    and n.args[0].id in ns and isinstance(n.args[1], ast.Constant) \
                    and isinstance(n.args[1].value, str):
                continue
            # setattr mutates the namespace; it does not read the flag. Exclude
            # only that first namespace argument from the escape check so a
            # namespace passed as the value still fails closed.
            args_to_check = list(n.args)
            if isinstance(n.func, ast.Name) and n.func.id == "setattr" \
                    and len(n.args) >= 2 and isinstance(n.args[0], ast.Name) \
                    and n.args[0].id in ns:
                args_to_check = list(n.args[1:])
            for arg in args_to_check + [k.value for k in n.keywords]:
                if isinstance(arg, ast.Name) and arg.id in ns:
                    return "namespace passed to another call"
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id in ns:
            return "namespace returned"
    return None


def analyse_source(src: str, label: str = "<src>") -> tuple[list[dict], str | None]:
    """Return (findings, skip_reason). A skip_reason means nothing was proven
    about this file -- it is NOT the same as a clean result."""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [], f"syntax error: {exc.msg}"
    except RecursionError as exc:
        return [], f"analysis failed: {type(exc).__name__}: {exc}"

    declared = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            d, flag = dest_for(node)
            if d:
                declared.append((d, flag, node.lineno))
    if not declared:
        return [], None

    reason = unprovable(tree)
    if reason:
        return [], reason

    ns = _namespace_names(tree)
    reads = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
             and isinstance(n.value, ast.Name) and n.value.id in ns}
    # AugAssign targets have Store context, but += and friends read the old value.
    reads.update(n.target.attr for n in ast.walk(tree)
                 if isinstance(n, ast.AugAssign)
                 and isinstance(n.target, ast.Attribute)
                 and isinstance(n.target.value, ast.Name)
                 and n.target.value.id in ns)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("getattr", "hasattr") and len(n.args) >= 2 \
                and isinstance(n.args[0], ast.Name) and n.args[0].id in ns:
            second = n.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                reads.add(second.value)

    return ([{"file": label, "flag": flag, "dest": d, "line": line}
             for d, flag, line in declared if d not in reads], None)


def _scan_counted(root: Path) -> tuple[list[dict], list[dict], int, bool]:
    findings: list[dict] = []
    skipped: list[dict] = []
    candidates = 0
    single_file = root.is_file()
    files = [root] if single_file else sorted(root.rglob("*.py"))
    for p in files:
        if any(part in {"__pycache__", ".git", ".venv", "venv",
                        "node_modules", "site-packages"} for part in p.parts):
            continue
        candidates += 1
        try:
            rel = str(p.relative_to(root if root.is_dir() else root.parent))
        except ValueError:
            rel = str(p)
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            skipped.append({"file": rel,
                            "reason": f"unreadable: {type(exc).__name__}: {exc}"})
            continue
        try:
            found, reason = analyse_source(src, rel)
        except Exception as exc:
            found, reason = [], f"analysis failed: {type(exc).__name__}: {exc}"
        findings.extend(found)
        if reason:
            skipped.append({"file": rel, "reason": reason})
    return findings, skipped, candidates, single_file


def scan(root: Path) -> tuple[list[dict], list[dict]]:
    findings, skipped, _, _ = _scan_counted(root)
    return findings, skipped


def _sarif_uri(label: str) -> str:
    # Path understands this host's separators; a POSIX literal backslash must
    # remain a filename character rather than becoming a directory separator.
    return quote_from_bytes(os.fsencode(Path(label).as_posix()), safe="/")


def _sarif_report(findings: list[dict], skipped: list[dict], candidates: int,
                  *, input_failed: bool = False, discovery_complete: bool = False) -> dict:
    """Project one scan to SARIF without source snippets or absolute roots.

    Invocation success records tool execution, not a clean/complete analysis.
    Existing skip and exit-code policies stay separate from this projection.
    """
    notifications: list[dict] = []
    execution_successful = not input_failed
    for row in skipped:
        reason = row["reason"]
        if reason.startswith("unreadable:"):
            category = "unreadable"
            message = "This source could not be read; the file was not checked."
        elif reason.startswith("syntax error:"):
            category = "syntax-error"
            message = "This source could not be parsed; the file was not checked."
        elif reason.startswith("analysis failed:"):
            category = "analysis-failed"
            message = "Analysis did not complete; the file was not checked."
        else:
            category = "unprovable"
            message = "Namespace reads cannot be proven statically; the file was not checked."
        failed = category != "unprovable"
        execution_successful = execution_successful and not failed
        notifications.append({
            "descriptor": {"id": "NF-ANALYSIS-ERROR" if failed else "NF-SKIPPED"},
            "level": "error" if failed else "warning",
            "message": {"text": message},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": _sarif_uri(row["file"])},
            }}],
            "properties": {"category": category},
        })

    analyzed = candidates - len(skipped)
    if input_failed:
        coverage = "unavailable"
        notifications.append({
            "descriptor": {"id": "NF-ANALYSIS-ERROR"},
            "level": "error",
            "message": {"text": "The input path was unavailable or traversal failed; "
                        "no coverage is established."},
        })
    elif analyzed == 0:
        coverage = "none"
        notifications.append({
            "descriptor": {"id": "NF-NO-ANALYSIS"},
            "level": "warning",
            "message": {"text": "No eligible Python file was analyzed; "
                        "empty results do not establish a clean scan."},
        })
    else:
        coverage = "partial" if skipped else "complete"

    if not input_failed and not discovery_complete:
        notifications.append({
            "descriptor": {"id": "NF-DISCOVERY-UNKNOWN"},
            "level": "warning",
            "message": {"text": "Directory enumeration is not certified by this scanner; "
                        "full traversal coverage is unknown."},
        })

    results = [{
        "ruleId": "NF001",
        "ruleIndex": 0,
        "level": "warning",
        "message": {"text": f"Argument {row['flag']!r} is accepted but its destination "
                    f"{row['dest']!r} is never read in this file."},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": _sarif_uri(row["file"])},
            "region": {"startLine": row["line"]},
        }}],
    } for row in findings]
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "noop-flags",
                "semanticVersion": __version__,
                "informationUri": "https://github.com/nymrel/noop-flags",
                "rules": [{
                    "id": "NF001",
                    "name": "AcceptedButUnusedArgument",
                    "shortDescription": {"text": "CLI argument accepted but never read"},
                    "fullDescription": {"text": "Single-file argparse analysis found a declared "
                                        "argument destination with no provable read. Dynamic "
                                        "namespace cases are skipped; conditional reads and "
                                        "other parsers are outside this check."},
                    "defaultConfiguration": {"level": "warning"},
                    "helpUri": "https://github.com/nymrel/noop-flags#what-it-refuses-to-answer",
                    "properties": {"tags": ["correctness", "cli"]},
                }],
                "notifications": [
                    {"id": "NF-SKIPPED",
                     "shortDescription": {"text": "Source analysis was deliberately skipped"}},
                    {"id": "NF-ANALYSIS-ERROR",
                     "shortDescription": {"text": "Source analysis could not complete"}},
                    {"id": "NF-NO-ANALYSIS", "shortDescription": {"text": "No source was analyzed"}},
                    {"id": "NF-DISCOVERY-UNKNOWN",
                     "shortDescription": {"text": "Directory traversal coverage is unknown"}},
                ],
            }},
            "invocations": [{
                "executionSuccessful": execution_successful,
                "toolExecutionNotifications": notifications,
            }],
            "results": results,
            "properties": {
                "candidateFiles": candidates,
                "analyzedFiles": analyzed,
                "skippedFiles": len(skipped),
                "analysisCoverage": coverage,
                "discoveryCoverage": ("unavailable" if input_failed else
                                      "complete" if discovery_complete else "unknown"),
                "scope": "Eligible source files encountered by the existing walk; "
                         "single-file argparse reads only.",
            },
        }],
    }


# --------------------------------------------------------------------------
# Proof. Each case is a shape this tool got wrong on real code at least once.
# --------------------------------------------------------------------------

_BASE = '''
import argparse
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product")
    ap.add_argument("--used")
    args = ap.parse_args()
    print(args.used)
'''


def _cases() -> list[tuple[str, str, list[str], str | None]]:
    sub = lambda body: _BASE.replace("print(args.used)", body)  # noqa: E731
    return [
        ("accepted-and-ignored flag IS reported",
         _BASE, ["product"], None),
        ("dynamic getattr -> refuse to guess",
         sub('f="product"\n    print(getattr(args, f))'), [], "dynamic getattr/hasattr"),
        ("namespace passed to a call -> refuse",
         sub("helper(args)"), [], "namespace passed to another call"),
        ("vars(args) -> refuse",
         sub("print(vars(args))"), [], "vars(namespace)"),
        ("namespace returned -> refuse",
         sub("return args"), [], "namespace returned"),
        ("args.__dict__ -> refuse",
         sub("print(args.__dict__)"), [], "namespace.__dict__"),
        ("getattr with a LITERAL name counts as a read",
         sub('print(getattr(args, "product"))'), ["used"], None),
        ("augmented assignment reads its target but not a sibling", '''
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--count", type=int, default=0)
ap.add_argument("--unused")
args = ap.parse_args()
args.count += 1
''', ["unused"], None),
        ("literal setattr is a write, not a read",
         sub('setattr(args, "product", "override")\n    print(args.used)'), ["product"], None),
        ("dynamic setattr is still a write, not an escape",
         sub('field="product"\n    setattr(args, field, "override")\n    print(args.used)'),
         ["product"], None),
        ("hasattr with a LITERAL name counts as a read",
         sub('print(hasattr(args, "product"))\n    print(args.used)'), [], None),
        ("explicit dest= is honoured, not the flag name", '''
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--thing", dest="renamed")
args = ap.parse_args()
print(args.renamed)
''', [], None),
        ("dashes become underscores like argparse does", '''
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()
''', ["dry_run"], None),
        ("action=version is argparse's own, not an ignored flag", '''
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--version", action="version", version="1.0")
ap.add_argument("--real")
args = ap.parse_args()
''', ["real"], None),
        ("a file with no add_argument is not a finding and not a skip",
         "print('hello')", [], None),
        ("unparseable file is SKIPPED, never reported clean",
         "def (:", [], "syntax error"),
    ]


def selftest(as_json: bool) -> int:
    results = []
    for label, src, want_flags, want_skip in _cases():
        found, reason = analyse_source(src)
        got = sorted(f["dest"] for f in found)
        skip_ok = (reason is None if want_skip is None
                   else (reason or "").startswith(want_skip))
        ok = got == sorted(want_flags) and skip_ok
        results.append({"label": label, "expected": sorted(want_flags),
                        "actual": got, "expected_skip": want_skip,
                        "actual_skip": reason, "ok": ok})

    passed = sum(1 for r in results if r["ok"])
    if as_json:
        print(json.dumps({"passed": passed, "total": len(results),
                          "results": results}, indent=2))
    else:
        print("=" * 78)
        print("noop-flags proof -- catch the real shape, refuse the unprovable ones")
        print("=" * 78)
        for r in results:
            print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['label']:<52} "
                  f"want={r['expected']} got={r['actual']}")
        print(f"\n  {passed}/{len(results)} proved")
    return 0 if passed == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="noop-flags",
        description="Find CLI flags that are accepted and never read.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".",
                    help="file or directory to scan (default: the current directory)")
    formats = ap.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true", help="machine-readable output")
    formats.add_argument("--sarif", action="store_true", help="SARIF 2.1.0 output with explicit coverage diagnostics")
    ap.add_argument("--show-skipped", action="store_true",
                    help="list files whose reads could not be proven statically")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the analyser against its own known-tricky cases")
    ap.add_argument("--version", action="version", version=f"noop-flags {__version__}")
    args = ap.parse_args(argv)

    if args.sarif and args.selftest:
        ap.error("--sarif describes source scans; use --json for selftest output")
    if args.selftest:
        return selftest(args.json)

    try:
        root = Path(args.path).expanduser().resolve()
        available = root.exists()
    except (OSError, RuntimeError):
        if not args.sarif:
            raise
        print(json.dumps(_sarif_report([], [], 0, input_failed=True), indent=2))
        return 2
    if not available:
        if args.sarif:
            print(json.dumps(_sarif_report([], [], 0, input_failed=True), indent=2))
            return 2
        print(f"cannot scan: no such path: {root}", file=sys.stderr)
        return 2

    if args.sarif:
        try:
            findings, skipped, candidates, single_file = _scan_counted(root)
        except OSError:
            print(json.dumps(_sarif_report([], [], 0, input_failed=True), indent=2))
            return 2
        print(json.dumps(_sarif_report(findings, skipped, candidates,
                                      discovery_complete=single_file), indent=2))
        return 1 if findings else 0

    findings, skipped = scan(root)

    if args.json:
        print(json.dumps({"root": str(root), "findings": findings,
                          "skipped": skipped}, indent=2))
        return 1 if findings else 0

    if not findings:
        print(f"no accepted-but-ignored parameters under {root}")
    else:
        by: dict[str, list[dict]] = {}
        for f in findings:
            by.setdefault(f["file"], []).append(f)
        print(f"{len(findings)} accepted-but-ignored parameter(s) "
              f"in {len(by)} file(s):\n")
        for f, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
            print(f"  {f}")
            for r in rows:
                print(f"      {r['flag']:<28} -> args.{r['dest']}  "
                      f"(declared line {r['line']}, never read)")
        print()

    if skipped:
        # Not a pass. These files were not checked, and saying so is the point.
        print(f"{len(skipped)} file(s) SKIPPED -- reads not statically provable, "
              f"so nothing was checked in them"
              f"{'' if args.show_skipped else ' (--show-skipped to list)'}")
        if args.show_skipped:
            for s in skipped:
                print(f"      {s['file']}: {s['reason']}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
