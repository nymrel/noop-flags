# Release policy

`noop-flags` is currently a source release candidate. Creating a GitHub release
or publishing to PyPI is a separate, reviewed action.

## Versioning

- Patch: compatible correctness, packaging, or documentation fixes.
- Minor: a new supported `argparse` shape backed by external fixtures and
  regression evidence.
- Major: a deliberate change to output, exit-code, or analysis contracts.

Supporting another parser or language is not automatically a minor feature. It
requires a separate precision evaluation and an explicit claim decision.

## Candidate checklist

1. Confirm the version matches in `noop_flags.py` and `pyproject.toml`.
2. Run unit tests, self-test, own-source dogfood, and `release_proof.py`.
3. Inspect the generated JSON proof and archive member lists.
4. Run the standard-library quietness regression named in the README and record
   the Python version and result.
5. Scan the full Git history and candidate archives for secrets, private paths,
   customer data, and proprietary source.
6. Review README commands, limitations, license, security route, and changelog.
7. Obtain an independent cross-team proof-read.
8. Create an annotated tag. Do not replace artifacts for an existing tag.
9. Create the GitHub release and verify every attached artifact and checksum.
10. Publish to PyPI only if the separate gate in
    [docs/PYPI_DECISION.md](docs/PYPI_DECISION.md) is approved.

## Rollback

Before publication, abandon or revert the candidate branch. After publication,
document a material defect and publish a corrected version. If a PyPI package
is unsafe or unusable, yank it with a reason; do not silently overwrite a
released artifact.
