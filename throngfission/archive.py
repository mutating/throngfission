import tarfile
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, List, Optional, Union

from dirstree import Crawler


def read_directory(path: Path, exclude: Optional[List[str]] = None) -> bytes:
    """Produce the same TAR representation as throng's temporary_directory."""
    buffer = BytesIO()
    write_directory(path, buffer, exclude)
    return buffer.getvalue()


def write_directory(path: Path, stream: BinaryIO, exclude: Optional[List[str]] = None) -> None:
    """Write a snapshot to a stream without buffering the archive in memory."""
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for file in Crawler(path, exclude=exclude):
            archive.add(file, arcname=file.relative_to(path))


def restore_directory(state: Union[bytes, BinaryIO], path: Path) -> None:
    """Extract workspace entries without writing outside the workspace."""
    root = path.resolve()
    with tarfile.open(fileobj=BytesIO(state) if isinstance(state, bytes) else state, mode='r:*') as archive:
        for member in archive:
            destination = root / member.name
            destination.resolve().relative_to(root)
            if member.issym():
                (destination.parent / member.linkname).resolve().relative_to(root)
            elif member.islnk():
                (root / member.linkname).resolve().relative_to(root)
            elif not (member.isfile() or member.isdir()):
                raise ValueError('Only files, directories and internal links are supported.')
            archive.extract(member, path=root)
