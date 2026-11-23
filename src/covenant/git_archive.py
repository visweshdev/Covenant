"""Extract data from a Git archive into a dedicated working directory."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path


def extract_git_archive(data: bytes, destination: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(destination, filter="data")
            return

        # Early Python 3.11 builds lack the data filter. Only materialize regular files and directories.
        root = destination.resolve()
        for member in archive:
            target = (root / member.name).resolve()
            if not target.is_relative_to(root) or not (
                member.isfile() or member.isdir()
            ):
                raise ValueError(f"unsupported entry in Git archive: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"missing data in Git archive: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
