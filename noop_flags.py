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
  python noop_flags.py --require-complete
  python noop_flags.py --show-skipped
  python noop_flags.py --selftest

Default exit codes:
  0  no ignored parameters found
  1  at least one flag is accepted and never read
  2  the scan could not run (bad path)

With --require-complete, incomplete scoped scans exit 2 before findings (1)
or a completed scan without findings (0). See README for its source scope.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import io
import json
import os
import stat
import sys
import tokenize
from pathlib import Path
from urllib.parse import quote_from_bytes

__version__ = "0.1.0"

_EXCLUDED_PARTS = {"__pycache__", ".git", ".venv", "venv",
                   "node_modules", "site-packages"}


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
        if any(part in _EXCLUDED_PARTS for part in p.parts):
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


def _is_link(info: os.stat_result) -> bool:
    # Refuse Windows directory junctions and other reparse points too.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _checked_target(text: str) -> tuple[Path | None, str | None]:
    """Inspect lexical components before resolve() can erase a root link."""
    try:
        lexical = Path(text).expanduser().absolute()
        current = Path(lexical.anchor)
        for part in lexical.parts[1:]:
            current /= part
            if _is_link(current.lstat()):
                return None, "symlink-target"
        return lexical.resolve(), None
    except (OSError, RuntimeError):
        return None, "input-unavailable"


class _SourceChanged(ValueError):
    pass


class _UnsupportedSource(ValueError):
    pass


def _complete_scan(root: Path | None, input_issue: str | None = None
                   ) -> tuple[list[dict], list[dict], dict]:
    """Checked, no-follow enumeration and observed stable source analysis.

    This is an opt-in scoped gate, not an atomic filesystem snapshot or sandbox.
    The legacy walker and pure analyzer retain their existing behavior.
    """
    findings: list[dict] = []
    skipped: list[dict] = []
    discovery: list[dict] = []
    files: list[tuple[Path, os.stat_result | None]] = []
    directories: dict[str, tuple[Path, os.stat_result]] = {}

    def label(path: Path) -> str:
        assert root is not None
        base = root if directories else root.parent
        try:
            return str(path.relative_to(base))
        except ValueError:
            return "."

    def problem(path: Path | None, category: str, message: str) -> None:
        discovery.append({"file": label(path) if path is not None else ".",
                          "category": category, "message": message})

    def read_source(path: Path, expected: os.stat_result) -> str:
        if _is_link(expected) or not stat.S_ISREG(expected.st_mode):
            raise _UnsupportedSource
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if _signature(os.fstat(stream.fileno())) != _signature(expected):
                raise _SourceChanged
            raw = stream.read()
            if _signature(os.fstat(stream.fileno())) != _signature(expected):
                raise _SourceChanged
        if _signature(path.lstat()) != _signature(expected):
            raise _SourceChanged
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        return raw.decode(encoding)

    if input_issue or root is None:
        problem(None, input_issue or "input-unavailable",
                "Input could not be resolved without links.")
    elif any(part in _EXCLUDED_PARTS for part in root.parts):
        problem(None, "excluded-target", "The target is inside a documented excluded directory.")
    else:
        try:
            info = root.lstat()
            if _is_link(info):
                problem(None, "symlink-target", "A linked target cannot satisfy the checked gate.")
            elif stat.S_ISREG(info.st_mode):
                files.append((root, info))
            elif stat.S_ISDIR(info.st_mode):
                directories[str(root)] = (root, info)

                def walk_error(error: OSError) -> None:
                    path = Path(error.filename) if error.filename else root
                    problem(path, "traversal-error", "Directory traversal could not complete.")

                for directory, names, filenames in os.walk(root, topdown=True,
                                                           onerror=walk_error, followlinks=False):
                    here = Path(directory)
                    try:
                        observed = here.lstat()
                        entry = directories.get(str(here))
                        expected = entry[1] if entry is not None else None
                        if (expected is None or _is_link(observed)
                                or _signature(observed) != _signature(expected)):
                            raise ValueError("directory changed")
                    except (OSError, ValueError):
                        problem(here, "directory-changed", "A directory changed during traversal.")
                        names[:] = []
                        continue

                    retained = []
                    for name in sorted(names):
                        if name in _EXCLUDED_PARTS:
                            continue
                        path = here / name
                        try:
                            child = path.lstat()
                            if _is_link(child):
                                problem(path, "linked-directory",
                                        "A linked directory was not followed.")
                            elif not stat.S_ISDIR(child.st_mode):
                                problem(path, "directory-changed",
                                        "A directory entry changed during traversal.")
                            else:
                                directories[str(path)] = (path, child)
                                retained.append(name)
                        except OSError:
                            problem(path, "traversal-error", "A directory could not be inspected.")
                    names[:] = retained

                    for name in sorted(filenames):
                        if not fnmatch.fnmatch(name, "*.py"):
                            continue
                        path = here / name
                        try:
                            files.append((path, path.lstat()))
                        except OSError:
                            # A selected source that vanished is still a candidate.
                            files.append((path, None))
            else:
                problem(None, "unsupported-target", "The target is not a regular file or directory.")
        except OSError:
            problem(None, "input-unavailable", "The target could not be inspected.")

    stable: list[tuple[Path, os.stat_result]] = []
    for path, expected in sorted(files, key=lambda item: str(item[0])):
        rel = label(path)
        try:
            if expected is None:
                raise OSError("source unavailable")
            src = read_source(path, expected)
        except _SourceChanged:
            skipped.append({"file": rel, "reason": "source changed: metadata mismatch"})
            continue
        except _UnsupportedSource:
            skipped.append({"file": rel, "reason": "unsupported source: regular files required"})
            continue
        except (OSError, UnicodeError, SyntaxError, LookupError):
            skipped.append({"file": rel, "reason": "unreadable: source could not be decoded or read"})
            continue
        try:
            found, reason = analyse_source(src, rel)
        except Exception:
            found, reason = [], "analysis failed: analyzer did not complete"
        if reason:
            skipped.append({"file": rel, "reason": reason})
        else:
            findings.extend(found)
            stable.append((path, expected))

    # Detect common changes after enumeration/read/analysis. This is best-effort,
    # not an atomic snapshot and not protection against a hostile filesystem.
    for path, expected in stable:
        try:
            changed = _signature(path.lstat()) != _signature(expected)
        except OSError:
            changed = True
        if changed:
            rel = label(path)
            findings[:] = [row for row in findings if row["file"] != rel]
            skipped.append({"file": rel, "reason": "source changed: metadata mismatch"})
    for path, expected in directories.values():
        try:
            changed = _signature(path.lstat()) != _signature(expected)
        except OSError:
            changed = True
        if changed:
            problem(path, "directory-changed", "A directory changed during the scan.")

    candidates = len(files)
    analyzed = candidates - len(skipped)
    complete = not discovery and not skipped and analyzed > 0
    code = 2 if not complete else 1 if findings else 0
    coverage = {"schema": "noop-flags.complete-scan.v1", "required": True,
                "complete": complete, "discoveryComplete": not discovery,
                "candidateFiles": candidates, "analyzedFiles": analyzed,
                "skippedFiles": len(skipped), "discoveryDiagnostics": discovery,
                "exitCode": code}
    return findings, skipped, coverage


def _sarif_uri(label: str) -> str:
    # Path understands this host's separators; a POSIX literal backslash must
    # remain a filename character rather than becoming a directory separator.
    return quote_from_bytes(os.fsencode(Path(label).as_posix()), safe="/")


def _sarif_report(findings: list[dict], skipped: list[dict], candidates: int,
                  *, input_failed: bool = False, discovery_complete: bool = False,
                  complete_scan: dict | None = None) -> dict:
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
        elif reason.startswith("source changed:"):
            category = "source-changed"
            message = "The source changed during the checked scan; its findings were discarded."
        elif reason.startswith("unsupported source:"):
            category = "unsupported-source"
            message = "The selected source is not a supported regular file; it was not checked."
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

    if not input_failed and not discovery_complete and complete_scan is None:
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
    report = {
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
                                      "complete" if discovery_complete else
                                      "incomplete" if complete_scan is not None else "unknown"),
                "scope": "Eligible source files encountered by the existing walk; "
                         "single-file argparse reads only.",
            },
        }],
    }
    if complete_scan is not None:
        run = report["runs"][0]
        run["properties"]["completeScan"] = complete_scan
        run["properties"]["scope"] = (
            "Eligible regular Python sources under checked traversal; "
            "documented exclusions and mutation limitations apply."
        )
        invocation = run["invocations"][0]
        invocation["exitCode"] = complete_scan["exitCode"]
        for row in complete_scan["discoveryDiagnostics"]:
            invocation["executionSuccessful"] = False
            invocation["toolExecutionNotifications"].append({
                "descriptor": {"id": "NF-ANALYSIS-ERROR"},
                "level": "error",
                "message": {"text": row["message"]},
                "locations": [{"physicalLocation": {
                    "artifactLocation": {"uri": _sarif_uri(row["file"])},
                }}],
                "properties": {"category": row["category"]},
            })
    return report


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


def _print_scan(root: Path | None, findings: list[dict], skipped: list[dict],
                show_skipped: bool, *, incomplete: bool = False) -> None:
    if not findings:
        if incomplete:
            print("No complete finding verdict is available for this scoped input.")
        else:
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
        print(f"{len(skipped)} file(s) SKIPPED -- reads not statically provable, "
              f"so nothing was checked in them"
              f"{'' if show_skipped else ' (--show-skipped to list)'}")
        if show_skipped:
            for s in skipped:
                print(f"      {s['file']}: {s['reason']}")


def _complete_cli(path_text: str, as_json: bool, as_sarif: bool,
                  show_skipped: bool) -> int:
    root, issue = _checked_target(path_text)
    findings, skipped, coverage = _complete_scan(root, issue)
    if as_sarif:
        print(json.dumps(_sarif_report(
            findings, skipped, coverage["candidateFiles"],
            input_failed=root is None,
            discovery_complete=coverage["discoveryComplete"],
            complete_scan=coverage,
        ), indent=2))
    elif as_json:
        print(json.dumps({"root": str(root) if root is not None else None,
                          "findings": findings, "skipped": skipped,
                          "coverage": coverage}, indent=2))
    else:
        state = ("INCOMPLETE" if not coverage["complete"]
                 else "FINDINGS" if findings else "PASS")
        print(f"Complete-scan gate: {state}; exit {coverage['exitCode']}; "
              f"{coverage['analyzedFiles']}/{coverage['candidateFiles']} source(s) analyzed")
        for row in coverage["discoveryDiagnostics"]:
            print(f"  {row['file']}: {row['message']}")
        _print_scan(root, findings, skipped, show_skipped,
                    incomplete=not coverage["complete"])
    return coverage["exitCode"]


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
    ap.add_argument("--require-complete", action="store_true",
                    help="require checked scoped traversal and analysis before exit 0/1")
    ap.add_argument("--version", action="version", version=f"noop-flags {__version__}")
    args = ap.parse_args(argv)

    if args.sarif and args.selftest:
        ap.error("--sarif describes source scans; use --json for selftest output")
    if args.require_complete and args.selftest:
        ap.error("--require-complete describes source scans, not detector selftests")
    if args.selftest:
        return selftest(args.json)

    if args.require_complete:
        return _complete_cli(args.path, args.json, args.sarif, args.show_skipped)

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

    _print_scan(root, findings, skipped, args.show_skipped)

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
