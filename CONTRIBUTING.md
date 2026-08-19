# Contributing

`noop-flags` deliberately keeps a narrow claim. Contributions should improve evidence, precision, portability, or maintainability before they expand language or parser coverage.

## Before opening a change

Run:

```bash
python noop_flags.py --selftest
python -m unittest -v
python noop_flags.py noop_flags.py
```

For behavior changes, add tests in both directions when possible: prove the broken shape is detected and prove the corresponding valid shape is not reported.

## Precision rule

A new detector must not convert an unprovable case into a confident finding. When static reads escape the supported analysis boundary, prefer an explicit skip over a speculative result.

Any change that increases recall must document the new false-positive surface and include adversarial fixtures for it.

## Scope changes

Support for another parser (`click`, `typer`, etc.), another language, inter-module data flow, or conditional-read analysis is a product-scope change rather than a small parser patch. Open an issue with:

- the exact new claim;
- known ambiguous shapes;
- proposed refusal/skip behavior;
- a fixture corpus;
- how precision will be measured before release.

## Pull requests

Keep PRs small and explain:

- the failure mode being addressed;
- the smallest reproducer;
- tests added or changed;
- any new limitation;
- whether the public claim or README needs to change.

Do not add runtime dependencies without demonstrating why the same result cannot reasonably remain dependency-free.
