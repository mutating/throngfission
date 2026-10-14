import base64
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from io import BytesIO
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from throngfission.execution.package import worker_archive


def test_worker_bundle_is_deterministic():
    """The installation fingerprint is stable and the worker carries skelet."""
    first = worker_archive()
    assert first == worker_archive()
    with ZipFile(BytesIO(base64.b64decode(first))) as archive:
        for package in ('throngfission', 'throngfission/execution', 'throngfission/execution/worker'):
            assert archive.read(f'{package}/__init__.py') == b''
        assert {name for name in archive.namelist() if name.startswith('throngfission/')} == {
            'throngfission/__init__.py',
            'throngfission/execution/__init__.py',
            'throngfission/execution/archive.py',
            'throngfission/execution/protocol.py',
            'throngfission/execution/worker/__init__.py',
            'throngfission/execution/worker/runtime.py',
            'throngfission/execution/worker/janitor.py',
            'throngfission/execution/worker/settings.py',
        }
        assert 'skelet/storage.py' in archive.namelist()
        assert 'throngfission/execution/worker/janitor.py' in archive.namelist()
        assert any('LICENSE' in name for name in archive.namelist())
        assert not any(name.endswith(('.so', '.pyc')) for name in archive.namelist())


def test_optional_older_python_dependencies(monkeypatch):
    """Modern Python can bundle a worker without compatibility-only packages."""
    monkeypatch.setattr('throngfission.execution.package.distribution', Mock(side_effect=[*[Mock(files=None)] * 14, PackageNotFoundError('tomli'), PackageNotFoundError('typing_extensions')]))
    assert worker_archive()


def test_required_dependency_must_be_installed(monkeypatch):
    """Missing required source packages cannot produce a broken worker silently."""
    monkeypatch.setattr('throngfission.execution.package.distribution', Mock(side_effect=PackageNotFoundError('dirstree')))
    with pytest.raises(PackageNotFoundError):
        worker_archive()


def test_worker_imports_without_client_site_packages(tmp_path):
    """The ZIP contains the complete worker dependency graph, including janitor."""
    archive = tmp_path / 'worker.zip'
    archive.write_bytes(base64.b64decode(worker_archive()))
    result = subprocess.run([sys.executable, '-I', '-S', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from throngfission.execution.worker.runtime import Session; from throngfission.execution.worker.janitor import main; Session(4096).close()', str(archive)], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
