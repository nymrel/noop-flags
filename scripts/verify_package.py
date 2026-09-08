#!/usr/bin/env python3
"""Fail-closed source and distribution checks for noop-flags releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tarfile
import tomllib
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_VERSION_PATTERN = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


class VerificationError(RuntimeError):
    """Raised when source or package metadata drifts from the release contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source(root: Path = ROOT) -> str:
    source = (root / "noop_flags.py").read_text(encoding="utf-8")
    source_match = SOURCE_VERSION_PATTERN.search(source)
    require(source_match is not None, "noop_flags.py is missing a literal __version__")

    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata.get("project", {})
    build_system = metadata.get("build-system", {})

    version = project.get("version")
    require(version == source_match.group(1), "source and project versions disagree")
    require(project.get("name") == "noop-flags", "project name drifted")
    require(project.get("requires-python") == ">=3.11", "Python support floor drifted")
    require(
        project.get("dependencies") == [], "runtime dependency-free contract drifted"
    )
    require(
        project.get("scripts", {}).get("noop-flags") == "noop_flags:main",
        "console entry point drifted",
    )
    require(project.get("license") == "MIT", "SPDX license expression drifted")
    require(
        project.get("license-files") == ["LICENSE"], "license file declaration drifted"
    )
    require(
        build_system.get("requires") == ["hatchling==1.32.0"],
        "build backend identity drifted",
    )
    require(
        build_system.get("build-backend") == "hatchling.build", "build backend drifted"
    )
    require(
        (root / "LICENSE").read_text(encoding="utf-8").startswith("MIT License"),
        "LICENSE drifted",
    )
    return version


def verify_tag(tag: str, version: str) -> None:
    require(
        tag == f"v{version}", f"tag {tag!r} does not match package version v{version}"
    )


def verify_archive_path(name: str) -> None:
    require("\\" not in name, f"archive contains a non-POSIX path: {name}")
    path = PurePosixPath(name)
    require(not path.is_absolute(), f"archive contains an absolute path: {name}")
    require(".." not in path.parts, f"archive contains path traversal: {name}")


def verify_wheel(path: Path, version: str) -> dict[str, str]:
    require(path.is_file(), f"wheel does not exist: {path}")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        for entry in entries:
            verify_archive_path(entry.filename)
            require(
                not stat.S_ISLNK(entry.external_attr >> 16),
                f"wheel contains a link: {entry.filename}",
            )

        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        require(
            len(metadata_names) == 1, "wheel must contain exactly one METADATA file"
        )
        metadata = BytesParser(policy=policy.default).parsebytes(
            archive.read(metadata_names[0])
        )

        require("noop_flags.py" in names, "wheel is missing noop_flags.py")
        require(metadata.get("Name") == "noop-flags", "wheel project name drifted")
        require(metadata.get("Version") == version, "wheel version drifted")
        require(
            metadata.get("Requires-Python") == ">=3.11", "wheel Python floor drifted"
        )
        require(
            metadata.get("License-Expression") == "MIT", "wheel SPDX metadata drifted"
        )
        require(
            not metadata.get_all("Requires-Dist"),
            "wheel unexpectedly declares runtime dependencies",
        )
        require(
            any(name.endswith(".dist-info/entry_points.txt") for name in names),
            "wheel is missing console entry-point metadata",
        )
        require(
            any(".dist-info/licenses/LICENSE" in name for name in names),
            "wheel is missing its declared license file",
        )

    return {"path": path.name, "sha256": sha256(path)}


def verify_sdist(path: Path, version: str) -> dict[str, str]:
    require(path.is_file(), f"sdist does not exist: {path}")
    expected_root = f"noop_flags-{version}"
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        for member in members:
            verify_archive_path(member.name)
            require(
                not member.issym() and not member.islnk(),
                f"sdist contains a link: {member.name}",
            )
            require(
                member.isfile() or member.isdir(),
                f"sdist contains a non-file entry: {member.name}",
            )

        roots = {PurePosixPath(name).parts[0] for name in names}
        require(
            roots == {expected_root}, "sdist contains files outside its versioned root"
        )
        require(
            f"{expected_root}/noop_flags.py" in names, "sdist is missing noop_flags.py"
        )
        require(
            f"{expected_root}/pyproject.toml" in names,
            "sdist is missing pyproject.toml",
        )
        require(f"{expected_root}/LICENSE" in names, "sdist is missing LICENSE")

    return {"path": path.name, "sha256": sha256(path)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Require an exact v<project.version> tag")
    parser.add_argument("--wheel", type=Path, help="Inspect one built wheel")
    parser.add_argument(
        "--sdist", type=Path, help="Inspect one built source distribution"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = verify_source()
    if args.tag:
        verify_tag(args.tag, version)

    artifacts: list[dict[str, str]] = []
    if args.wheel:
        artifacts.append(verify_wheel(args.wheel.resolve(), version))
    if args.sdist:
        artifacts.append(verify_sdist(args.sdist.resolve(), version))

    print(
        json.dumps(
            {
                "artifacts": artifacts,
                "name": "noop-flags",
                "source_contract": "pass",
                "version": version,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except VerificationError as error:
        raise SystemExit(f"release verification failed: {error}") from error
