#!/usr/bin/env python3
"""Fail-closed source and distribution checks for noop-flags releases."""

from __future__ import annotations

import argparse
import configparser
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
HATCHLING_PIN_PATTERN = re.compile(r"^hatchling==1\.[0-9]+\.[0-9]+$")


class VerificationError(RuntimeError):
    """Raised when source or package metadata drifts from the release contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def verify_build_requirements(requirements: object) -> None:
    require(
        isinstance(requirements, list)
        and len(requirements) == 1
        and isinstance(requirements[0], str)
        and HATCHLING_PIN_PATTERN.fullmatch(requirements[0]) is not None,
        "build backend dependency must be a single exact hatchling release pin",
    )


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
    verify_build_requirements(build_system.get("requires"))
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


def verify_unique_paths(names: list[str], kind: str) -> None:
    normalized = [str(PurePosixPath(name)) for name in names]
    require(len(normalized) == len(set(normalized)), f"{kind} contains duplicate paths")


def verify_metadata(data: bytes, version: str, kind: str) -> None:
    metadata = BytesParser(policy=policy.default).parsebytes(data)
    for field, expected in (
        ("Name", "noop-flags"),
        ("Version", version),
        ("Requires-Python", ">=3.11"),
        ("License-Expression", "MIT"),
    ):
        require(
            metadata.get_all(field) == [expected], f"{kind} {field} metadata drifted"
        )
    require(
        not metadata.get_all("Requires-Dist"),
        f"{kind} unexpectedly declares runtime dependencies",
    )


def verify_entry_points(data: bytes) -> None:
    entry_points = configparser.ConfigParser(interpolation=None)
    entry_points.optionxform = str
    try:
        entry_points.read_string(data.decode("utf-8"))
    except (UnicodeDecodeError, configparser.Error) as error:
        raise VerificationError("wheel has invalid console entry-point metadata") from error
    require(
        not entry_points.defaults()
        and entry_points.sections() == ["console_scripts"]
        and dict(entry_points["console_scripts"]) == {"noop-flags": "noop_flags:main"},
        "wheel console entry point drifted",
    )


def verify_payload(data: bytes, filename: str, kind: str, root: Path) -> None:
    require(
        data == (root / filename).read_bytes(),
        f"{kind} {filename} differs from the source checkout",
    )


def verify_wheel(path: Path, version: str, root: Path = ROOT) -> dict[str, str]:
    require(path.is_file(), f"wheel does not exist: {path}")
    with ZipFile(path) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        verify_unique_paths(names, "wheel")
        for entry in entries:
            verify_archive_path(entry.filename)
            require(
                not stat.S_ISLNK(entry.external_attr >> 16),
                f"wheel contains a link: {entry.filename}",
            )

        dist_info = f"noop_flags-{version}.dist-info"
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        require(
            metadata_names == [f"{dist_info}/METADATA"],
            "wheel must contain exactly its versioned METADATA file",
        )
        verify_metadata(archive.read(metadata_names[0]), version, "wheel")

        for filename, source_filename in (
            ("noop_flags.py", "noop_flags.py"),
            (f"{dist_info}/licenses/LICENSE", "LICENSE"),
        ):
            require(filename in names, f"wheel is missing {filename}")
            verify_payload(archive.read(filename), source_filename, "wheel", root)

        entry_point_name = f"{dist_info}/entry_points.txt"
        require(entry_point_name in names, "wheel is missing console entry-point metadata")
        verify_entry_points(archive.read(entry_point_name))

    return {"path": path.name, "sha256": sha256(path)}


def verify_sdist(path: Path, version: str, root: Path = ROOT) -> dict[str, str]:
    require(path.is_file(), f"sdist does not exist: {path}")
    expected_root = f"noop_flags-{version}"
    with tarfile.open(path, mode="r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        verify_unique_paths(names, "sdist")
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
        by_name = {member.name: member for member in members}

        def read_file(filename: str) -> bytes:
            member = by_name.get(f"{expected_root}/{filename}")
            require(member is not None and member.isfile(), f"sdist is missing {filename}")
            handle = archive.extractfile(member)
            require(handle is not None, f"sdist cannot read {filename}")
            with handle:
                return handle.read()

        for filename in ("noop_flags.py", "pyproject.toml", "LICENSE"):
            verify_payload(read_file(filename), filename, "sdist", root)
        verify_metadata(read_file("PKG-INFO"), version, "sdist")

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
