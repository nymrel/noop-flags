# noop-flags

**Which of your CLI flags do nothing?**

`noop-flags` finds command-line flags your program *accepts* and then *never reads*.
No config, no dependencies, one file.

## Choose a starting point

| Your goal | Start here | Read the result |
| --- | --- | --- |
| Understand a report without coding | Read [what it refuses to answer](#what-it-refuses-to-answer) and share the flagged file, flag name, and declaration line with your developer. | A finding means a flag is declared but never read within the supported analysis. It is not a full audit of the application's behavior. No installation is needed to interpret an existing report. |
| Check a Python CLI | Run the source command below on a file or directory, with `--show-skipped` to see the analysis limits. | Exit `1` means findings. Exit `2` means the scan could not run. A skipped file was not checked; exit `0` alone does not establish a complete clean scan. |
| Automate with an agent or CI | Use `--json` for findings and skipped files, or [SARIF](#sarif-for-an-existing-code-review-workflow) for explicit coverage diagnostics. | Retain stdout even on exit `1`. Check skipped files and coverage before declaring success. The scan does not modify source or upload a report; apply any fixes through your existing review process. |

```bash
curl -sO https://raw.githubusercontent.com/nymrel/noop-flags/main/noop_flags.py
python noop_flags.py .
```

That is the whole setup. If it finds nothing, you get one line and exit 0. If it
finds something, you will probably not enjoy it:

```
21 accepted-but-ignored parameter(s) in 17 file(s):

  build-registry.py
      --check                      -> args.check  (declared line 59, never read)
  publish.py
      --dry-run                    -> args.dry_run  (declared line 24, never read)
```

A `--check` flag whose help text says *"dry run"* and which the program never
consults is not a dry run. It is a live run with a reassuring name.

---

## Why this exists

Three real bugs, all of them ours, all of them this exact shape.

**A search-index submitter shipped every public host to a third-party API on a
bare invocation.** It declared `--all-registered` to opt into the broad path, and
never read it. The broad path was simply what happened. Worse, its `--check`
flag — documented as a dry run — only suppressed *writing the report*, and did so
*after* the live HTTP requests had already gone out. Two flags, one file: one
that was supposed to arm the dangerous behaviour and didn't gate anything, and
one that was supposed to disarm it and fired too late to matter.

**A reaper treated `--dry-run --apply` as apply.** It accepted both flags and
read only `--apply`. Anyone belt-and-bracing a destructive command by adding
`--dry-run` to it got the destructive behaviour and a clean conscience.
(That one is fixed now; the fix is an explicit "you passed both, pick one" error.)

**A delivery verifier tested the same product on every run.** It accepted
`--product`, but read it only inside the `--ref` branch, so every local run
silently traversed the default product no matter what was asked for — and then
printed `PASS`. **Five of the six products in that storefront had no delivery
proof at all, while the harness reported that they did.**

Two of those landed on the same day. That is a class, not a coincidence — and it
is a class no linter was looking for. `ruff`'s `ARG001` catches unused *function*
arguments; `pyflakes` catches unused imports and locals. Nothing checks whether
the `--flag` you documented in `--help` is connected to anything.

## The failure mode this belongs to

A green check that never ran is worse than a red one. It does not merely fail to
find the problem — it actively buys false confidence, and it keeps buying it
every time it runs. An ignored flag is one member of a larger family:

- A guard that iterates a set and reports success. If the set is empty it
  reports the same success, having verified nothing.
- A caller that treats a return value as a success boolean when the callee
  returns an object. Objects are always truthy, so every failure reports success.
- A check whose data source has vanished, returning the neutral empty value —
  byte-identical to a source that was read and was genuinely empty. "Could not
  look" rendered as "looked and found nothing".
- A lead form that returns `{"success": true}` and sends zero email, because the
  send was never awaited and the serverless function exits when the response
  returns. The status code proved nothing; the mail provider's remaining-credit
  count, unchanged before and after, proved everything.
- A payment link that returns `200`. So does a deactivated one. So does a link ID
  that never existed — same status, byte-identical body, because the page
  resolves its state client-side after load. Monitoring payment links by status
  code tells you the CDN is up and nothing else.

`noop-flags` only addresses the first item on that list. It is a small tool with
a narrow claim, which is the only kind we're willing to ask you to trust.

## What it refuses to answer

The first version of this scan reported **75 candidates. 57 were wrong.**

Three shapes make a static read-check unsound, and when `noop-flags` sees one it
skips the entire file and *says so* rather than emitting a confident guess:

| Shape | Why it defeats the check |
|---|---|
| `getattr(args, name)` with a non-literal name | The attribute read is computed at runtime |
| The namespace passed to another call, or returned | The reads live in a file this scan never opened |
| `vars(args)` / `args.__dict__` | The whole namespace escapes as a dict |

Skipped files are reported separately and never counted as clean:

```
3 file(s) SKIPPED -- reads not statically provable, so nothing was checked
in them (--show-skipped to list)
```

That distinction is deliberate. A tool built to catch checks that pass without
exercising anything does not get to pass without exercising anything.

**Precision over recall.** A sweep that cries wolf gets ignored, and then the
real bug ships. `noop-flags` would rather stay quiet about a file it cannot
prove than hand you a list you learn to skim past.

## It found a bug in itself

The first time we ran `noop-flags` against its own source, it flagged its own
`--version`.

That was a false positive, and a real one: `action="version"` and `action="help"`
are consumed by `argparse` during parsing. Their `dest` exists on the namespace
and is never meant to be read. The fix is four lines, and there is a test named
after the incident.

When a class of bug is real, it shows up inside the machinery built to detect it.
Sweep your own instrument first.

## Usage

```bash
python noop_flags.py                    # scan the current directory
python noop_flags.py path/to/src        # scan a directory or a single file
python noop_flags.py --json             # machine-readable, for CI
python noop_flags.py --sarif            # SARIF 2.1.0, with coverage diagnostics
python noop_flags.py --show-skipped     # list what couldn't be proven, and why
python noop_flags.py --selftest         # prove the analyser on its known-tricky cases
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | No ignored parameters found |
| `1` | At least one flag is accepted and never read |
| `2` | The scan could not run (bad path) |

### SARIF for an existing code-review workflow

The `--sarif` reporter is an unreleased source addition. It emits the existing
detector's findings using SARIF 2.1.0; it adds no analyzer, account, service or
runtime dependency. From an exact source checkout, create a local report:

```bash
python noop_flags.py . --sarif > noop-flags.sarif
```

Exit `1` still means findings were produced, so retain the report even when a CI
step records failure. `--sarif` and `--json` are mutually exclusive. Selftests
retain their existing human/JSON output rather than pretending to scan a source
tree. Shell redirection controls the output file and may replace an existing
file; the scanner does not open a report path or upload anything.

Rule `NF001` is a correctness warning with the declaration's start line. Paths
are relative to the scan directory (or a single file's parent) and URI-encoded;
scan the repository root when the consumer expects repository-relative paths.
Only start lines are emitted, avoiding ambiguous Unicode column units. There are
no source snippets, absolute root path, vulnerability score or security claim.
File and argument names remain potentially sensitive metadata.

Every skipped file has a diagnostic with its relative location and a coarse
reason category. Raw parser/OS exception text is omitted. The run's properties
show `candidateFiles`, `analyzedFiles`, `skippedFiles` and `analysisCoverage`
(`complete`, `partial`, `none` or `unavailable`). Counts cover the existing
scanner's encountered eligible source files, not excluded directories, other
languages, cross-file reads or conditional execution. `complete` means all
encountered candidates were analyzed; it does not certify filesystem enumeration
or anything outside the scanner's existing scope. Zero analyzed sources is explicitly
reported; an empty results array does not establish a clean scan.

`discoveryCoverage` is a separate property. An explicit single-file target has
`complete` discovery, while directory discovery is always `unknown` with a
warning: the existing `Path.rglob` walk can silently omit unreadable directories.
A missing/unresolvable target is `unavailable`. Thus even complete analysis of
the encountered candidates does not certify a complete directory scan. Existing
human/JSON scanning behavior is unchanged.

`invocations[0].executionSuccessful` describes tool execution, not an all-source
quality verdict: read/parser/analysis errors make it false, while deliberate
dynamic-namespace refusals alone do not. Existing CLI exit policy is unchanged:
exit `0` means no findings, even if coverage is incomplete. Inspect diagnostics
and coverage properties when deciding whether a report is sufficient for a gate.

The format can be consumed by existing SARIF tools. GitHub code-scanning access,
upload permissions, root mapping, fingerprints and service limits are separate
consumer responsibilities; this change enables no workflow, upload or account
permission. GitHub uses a supported subset of SARIF and may not display every
coverage property. Retain the full report for incomplete-analysis diagnostics.

**In CI**

```yaml
- name: No accepted-but-ignored CLI flags
  run: python noop_flags.py .
```

## Limitations, and the false-positive numbers

A detector that cries wolf gets uninstalled, so here are the real numbers rather
than a claim.

**Where it started.** The first, unfiltered version of this scan reported **75
candidates on a 1,294-file tree. 57 were wrong** — a 76% false-positive rate.
Every one of those 57 was one of the three unprovable shapes in the table above.
That is why the refusals exist, and it is why the tool skips whole files.

**Where it is now.** On that same tree today it reports **21 findings and skips
157 files.** We hand-checked a six-finding sample from that run and all six were
real — flags genuinely declared and never read. Six is a sample, not a proof;
we are not claiming a zero false-positive rate, and if you find one we want the
issue.

**On code we did not write.** Run against the **Python 3.13 standard library** it
reports **zero findings** and skips 22 files. That is the result we wanted: on a
well-maintained codebase it stays quiet rather than manufacturing work.

**Note the shape of that trade.** 157 skipped against 21 reported means this tool
is *heavily* biased toward silence. It is not an audit and it will not tell you
your CLI is clean — it can only tell you about the files it could prove. That is
the honest ceiling, and `--show-skipped` exists so you can see it.

**The blind spot that will bite you.** `noop-flags` catches a flag read
*nowhere*. It does **not** catch a flag read in *one branch out of several* —
which is exactly the delivery-verifier bug above. That bug is the reason this
tool exists and this tool would not have caught it. We would rather say so than
let you infer a guarantee that isn't there.

## Scope, stated plainly

- **Python and `argparse` only.** Not `click`, not `typer`, not `docopt`, not
  other languages. Adding those means new false-positive classes and a bigger
  promise than we want to make; we would rather ship one thing that is right.
- **Single-file analysis.** If your parsing and your reads live in different
  modules, `noop-flags` will detect the namespace escaping and skip the file.
  It will tell you it did.
- **It cannot see conditional reads.** A flag read only inside `if
  platform == "win32"` counts as read. The delivery-verifier bug above — a flag
  read inside exactly one branch — is *not* something this tool catches. It
  catches the flag that is read *nowhere*.
- **`--selftest` proves the analyser, not your code.** They are different claims
  and the tool keeps them apart.

## Runtime and distribution status

The package contract supports maintained Python 3.11–3.14 releases. The
maintainer runtime is pinned in `.python-version` to Python 3.14.7, and CI
tests exact current patch releases on Linux and Windows.

This repository does not contain a PyPI publication step. Its release-evidence
workflow builds the wheel and source distribution, verifies their contents,
records SHA-256 digests, and creates GitHub provenance attestations. A package
registry binding, publication decision, tag, and public release remain separate
maintainer gates.

## Development

```bash
python scripts/verify_package.py
python -m unittest -v
python noop_flags.py --selftest
python noop_flags.py noop_flags.py
```

The tests assert in both directions where they can: that a genuinely ignored flag
is reported, *and* that the same flag goes quiet once it is read. A guard proven
only in the failing direction may be refusing everything. The release verifier
also fails closed on source/package version drift, unsupported runtime metadata,
runtime dependencies, unsafe archive paths, and missing license or entry-point
metadata.

## Licence

MIT. Use it, vendor it, paste it into your repo. If it finds something ugly,
we'd genuinely like to hear about it.

---

Built by [Nymrel](https://nymrel.com), out of the checks we run on our own work.
