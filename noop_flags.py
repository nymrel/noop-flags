#!/usr/bin/env python
"""noop-flags -- find the CLI flags your program accepts and never reads.

A flag the caller believes selects behaviour, which in fact selects nothing, is
a check that passes without exercising anything. The operator reads a green that
was never earned.

HOW IT DECIDES. For each argparse `add_argument`, derive the destination name
the way argparse itself does, then look for any read of that attribute anywhere
in the same file. Declared, never read -> reported.

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
import sys
from pathlib import Path

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
    conventions when the call is not a plain assignment."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f = n.value.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "parse_args":
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
    return names or {"args", "a", "ns", "opts"}


def unprovable(tree: ast.AST) -> str | None:
    """Return the reason this file's reads cannot be proven statically, or None."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id == "vars":
                return "vars(namespace)"
            if n.func.id in ("getattr", "setattr", "hasattr") and len(n.args) >= 2:
                second = n.args[1]
                if not (isinstance(second, ast.Constant)
                        and isinstance(second.value, str)):
                    return "dynamic getattr/setattr"
        if isinstance(n, ast.Attribute) and n.attr == "__dict__":
            return "namespace.__dict__"

    ns = _namespace_names(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            # getattr(args, "literal") passes the namespace but names exactly one
            # attribute, so it is a precise READ, not an escape. Without this
            # exemption the escape rule swallows the whole file and hides every
            # genuinely-ignored flag in it -- caught by this tool's own selftest.
            if isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "hasattr") \
                    and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                    and isinstance(n.args[1].value, str):
                continue
            for arg in list(n.args) + [k.value for k in n.keywords]:
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

    reads = {n.attr for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("getattr", "setattr", "hasattr") and len(n.args) >= 2:
            second = n.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                reads.add(second.value)

    return ([{"file": label, "flag": flag, "dest": d, "line": line}
             for d, flag, line in declared if d not in reads], None)


def scan(root: Path) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    skipped: list[dict] = []
    files = [root] if root.is_file() else sorted(root.rglob("*.py"))
    for p in files:
        if any(part in {"__pycache__", ".git", ".venv", "venv",
                        "node_modules", "site-packages"} for part in p.parts):
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(p.relative_to(root if root.is_dir() else root.parent))
        except ValueError:
            rel = str(p)
        found, reason = analyse_source(src, rel)
        findings.extend(found)
        if reason:
            skipped.append({"file": rel, "reason": reason})
    return findings, skipped


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
         sub('f="product"\n    print(getattr(args, f))'), [], "dynamic getattr/setattr"),
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
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--show-skipped", action="store_true",
                    help="list files whose reads could not be proven statically")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the analyser against its own known-tricky cases")
    ap.add_argument("--version", action="version", version=f"noop-flags {__version__}")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest(args.json)

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"cannot scan: no such path: {root}", file=sys.stderr)
        return 2

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
