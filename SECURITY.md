# Security policy

`noop-flags` is a static-analysis utility. It does not execute scanned Python files, import them, install their dependencies, or make network requests.

## Supported versions

The current `main` branch and latest tagged release, when one exists, are
supported on maintained Python 3.11–3.14 releases. Pre-release branches may
change without notice. A workflow artifact is release evidence, not a published
package or supported release.

## Report a security issue

Do not include secrets, private source code, customer data, or exploit material in a public issue.

Send a concise report to **contact@nymrel.com** with:

- affected version or commit;
- operating system and Python version;
- the smallest synthetic reproducer you can provide;
- expected and observed behavior;
- whether the issue could cause unsafe execution, path traversal, data disclosure, or a materially false analysis result.

A false-negative or false-positive that could create dangerous confidence is also worth reporting even when it is not a conventional security vulnerability.

## Security boundary

`noop-flags` only proves findings inside the narrow static shapes it supports. A clean scan is not a security audit and does not prove that every CLI flag is wired correctly. Files whose namespace usage cannot be statically proven are skipped and must not be counted as clean.
