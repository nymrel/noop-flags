# Contributing

Thank you for helping make `noop-flags` more trustworthy. Small, reproducible
changes are preferred over broad parser or language expansion.

## Good first contributions

- A sanitized fixture showing a true finding, false positive, false negative,
  or correctly skipped file.
- A test that proves both directions: the flag is reported while unread and
  becomes quiet when the destination is read.
- Documentation corrections that keep findings, skips, and clean scans
  distinct.
- Packaging or CI improvements that do not add runtime dependencies.

Do not submit private source, customer code, credentials, local absolute paths,
or incident data. A minimal synthetic fixture is usually better than an entire
repository.

## Before opening a pull request

```bash
python -m unittest -v
python noop_flags.py --selftest
python noop_flags.py noop_flags.py
```

If your change affects packaging, also run:

```bash
python -m pip install build
python release_proof.py
```

Explain the observed failure shape, include the smallest reproducer, and state
what the tool should report or refuse to classify. Pull requests that broaden
the product claim need external fixtures demonstrating acceptable precision.

By contributing, you agree that your contribution is licensed under this
repository's MIT License.
