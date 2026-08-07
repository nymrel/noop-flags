# noop-flags

**Which of your CLI flags do nothing?**

`noop-flags` finds command-line flags your program *accepts* and then *never reads*.
No config, no dependencies, one file.

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
`--product`, but only read it inside one branch. Every invocation printed `PASS`
for whichever product was the default, no matter what you asked it to check.

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
python noop_flags.py --show-skipped     # list what couldn't be proven, and why
python noop_flags.py --selftest         # prove the analyser on its known-tricky cases
```

**Exit codes**

| Code | Meaning |
|---|---|
| `0` | No ignored parameters found |
| `1` | At least one flag is accepted and never read |
| `2` | The scan could not run (bad path) |

**In CI**

```yaml
- name: No accepted-but-ignored CLI flags
  run: python noop_flags.py .
```

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

## Development

```bash
python -m unittest -v    # 29 tests, no dependencies
pytest                   # if you prefer
```

The tests assert in both directions where they can: that a genuinely ignored flag
is reported, *and* that the same flag goes quiet once it is read. A guard proven
only in the failing direction may be refusing everything.

## Licence

MIT. Use it, vendor it, paste it into your repo. If it finds something ugly,
we'd genuinely like to hear about it.

---

Built by [Nymrel](https://nymrel.com), out of the checks we run on our own work.
