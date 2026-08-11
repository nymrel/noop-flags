## What changed

Describe the smallest behavior or documentation change.

## Evidence

- [ ] `python -m unittest -v`
- [ ] `python noop_flags.py --selftest`
- [ ] `python noop_flags.py noop_flags.py`
- [ ] Packaging changes: `python release_proof.py`
- [ ] A finding test proves both the unread and read directions.

## Claim and safety check

- [ ] The narrow Python `argparse`, same-file claim is unchanged or narrower.
- [ ] Findings, skipped files, and clean scans remain distinct.
- [ ] No secrets, customer code, proprietary source, or private paths are included.
- [ ] Any new adoption or precision claim has a public, reproducible receipt.
