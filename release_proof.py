#!/usr/bin/env python
"""Build and prove the noop-flags release candidate without publishing it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
REQUIRED_REPO_FILES = {
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "RELEASING.md",
    "SECURITY.md",
    "noop_flags.py",
    "pyproject.toml",
}
ALLOWED_SDIST_FILES = REQUIRED_REPO_FILES | {
    ".gitignore",
    "PKG-INFO",
    "docs/ADOPTION_EVIDENCE.md",
    "docs/PYPI_DECISION.md",
    "release_proof.py",
    "test_noop_flags.py",
    "test_release_proof.py",
}
SENSITIVE_NAMES = {".env", "credentials", "id_rsa", "secrets.json"}


def run(command: list[str], *, cwd: Path | None = None,
        expected: int = 0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"command returned {result.returncode}, expected {expected}: "
            f"{command!r}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_member(member: str, root_name: str) -> str:
    pure = PurePosixPath(member)
    if pure.is_absolute() or ".." in pure.parts:
        raise RuntimeError(f"unsafe archive member: {member}")
    if not pure.parts or pure.parts[0] != root_name:
        raise RuntimeError(f"archive member outside expected root: {member}")
    relative = PurePosixPath(*pure.parts[1:]).as_posix()
    if not relative:
        return ""
    lowered = {part.lower() for part in PurePosixPath(relative).parts}
    if lowered & SENSITIVE_NAMES:
        raise RuntimeError(f"sensitive-looking archive member: {member}")
    return relative


def inspect_sdist(path: Path) -> list[str]:
    root_name = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, "r:gz") as archive:
        members = sorted(
            relative
            for member in archive.getmembers()
            if member.isfile()
            for relative in [safe_relative_member(member.name, root_name)]
            if relative
        )
    unexpected = sorted(set(members) - ALLOWED_SDIST_FILES)
    missing = sorted(REQUIRED_REPO_FILES - set(members))
    if unexpected or missing:
        raise RuntimeError(
            f"sdist contents failed: unexpected={unexpected}, missing={missing}"
        )
    return members


def inspect_wheel(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        members = sorted(name for name in archive.namelist() if not name.endswith("/"))
    if "noop_flags.py" not in members:
        raise RuntimeError("wheel does not contain noop_flags.py")
    top_level = [name for name in members if "/" not in name]
    if top_level != ["noop_flags.py"]:
        raise RuntimeError(f"wheel has unexpected top-level files: {top_level}")
    if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
           for name in members):
        raise RuntimeError("wheel contains an unsafe member path")
    return members


def venv_commands(environment: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe", environment / "Scripts" / "noop-flags.exe"
    return environment / "bin" / "python", environment / "bin" / "noop-flags"


def prove_install(wheel: Path, workspace: Path) -> dict[str, object]:
    environment = workspace / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python, command = venv_commands(environment)

    install = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-deps"]
    run(install + [str(wheel)], cwd=workspace)
    version = run([str(command), "--version"], cwd=workspace).stdout.strip()
    run([str(command), "--selftest"], cwd=workspace)

    fixture = workspace / "fixture.py"
    fixture.write_text(
        "import argparse\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--name')\n"
        "args = parser.parse_args()\n",
        encoding="utf-8",
    )
    finding = run([str(command), "--json", str(fixture)], cwd=workspace, expected=1)
    payload = json.loads(finding.stdout)
    if [row["dest"] for row in payload["findings"]] != ["name"]:
        raise RuntimeError(f"installed JSON contract mismatch: {payload}")

    fixture.write_text(fixture.read_text(encoding="utf-8") + "print(args.name)\n", encoding="utf-8")
    clean = run([str(command), str(fixture)], cwd=workspace)
    if "no accepted-but-ignored parameters" not in clean.stdout:
        raise RuntimeError("installed human-output clean contract mismatch")
    run([str(command), str(workspace / "missing.py")], cwd=workspace, expected=2)

    run([str(python), "-m", "pip", "uninstall", "-y", "noop-flags"], cwd=workspace)
    run([
        str(python), "-c",
        "import importlib.util; raise SystemExit(importlib.util.find_spec('noop_flags') is not None)",
    ], cwd=workspace)
    run(install + [str(wheel)], cwd=workspace)
    run([str(command), "--version"], cwd=workspace)

    return {
        "version_output": version,
        "console_entrypoint": str(command.relative_to(environment)),
        "json_finding_count": len(payload["findings"]),
        "uninstall_reinstall": "passed",
    }


def prove(repo: Path = ROOT) -> dict[str, object]:
    missing = sorted(name for name in REQUIRED_REPO_FILES if not (repo / name).is_file())
    if missing:
        raise RuntimeError(f"required repository files missing: {missing}")

    with tempfile.TemporaryDirectory(prefix="noop-flags-release-proof-") as raw_temp:
        workspace = Path(raw_temp)
        dist = workspace / "dist"
        run([sys.executable, "-m", "build", "--outdir", str(dist), str(repo)], cwd=workspace)
        wheels = sorted(dist.glob("*.whl"))
        sdists = sorted(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise RuntimeError(f"expected one wheel and one sdist, got {wheels} and {sdists}")
        wheel, sdist = wheels[0], sdists[0]
        wheel_members = inspect_wheel(wheel)
        sdist_members = inspect_sdist(sdist)
        install = prove_install(wheel, workspace)
        return {
            "status": "passed",
            "published": False,
            "artifacts": [
                {"name": wheel.name, "sha256": sha256(wheel), "members": wheel_members},
                {"name": sdist.name, "sha256": sha256(sdist), "members": sdist_members},
            ],
            "install": install,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the complete machine-readable receipt")
    args = parser.parse_args(argv)
    result = prove()
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("release proof: PASS")
        print(f"  published: {result['published']}")
        for artifact in result["artifacts"]:
            print(f"  {artifact['name']}: sha256={artifact['sha256']}")
        print(f"  installed command: {result['install']['version_output']}")
        print("  uninstall/reinstall: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
