# PyPI publication decision

**Status: HOLD. Do not publish automatically.**

The PyPI JSON endpoint for `noop-flags` returned HTTP 404 at
2026-08-11T13:00:59Z. That is a time-bounded availability check, not a
reservation, ownership receipt, or guarantee that the name remains available.

Publication remains blocked until all of these are true:

- the package name and exact maintainer account are rechecked;
- account recovery ownership is documented;
- PyPI Trusted Publishing is configured through GitHub OIDC, without a
  long-lived upload token;
- `release_proof.py` passes from a clean checkout;
- wheel and source-archive hashes and contents are recorded;
- the first version, changelog, rollback, and yank policy are reviewed;
- an independent cross-team reviewer approves the package and release path;
- the operator authorizes the first publication.

After publication, verify the package from PyPI in a new environment outside
the repository. Do not add a PyPI badge or `pip install noop-flags` command to
the README before that receipt exists.
