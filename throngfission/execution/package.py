import base64
from importlib.metadata import PackageNotFoundError, distribution
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def write(archive: ZipFile, name: str, data: bytes) -> None:
    entry = ZipInfo(name)
    entry.compress_type = ZIP_DEFLATED
    archive.writestr(entry, data)


def worker_archive() -> str:
    """Bundle the worker and its pure-Python dependencies once per installation."""
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        for name in ('throngfission', 'throngfission/execution', 'throngfission/execution/worker'):
            write(archive, f'{name}/__init__.py', b'')
        for name in ('protocol.py', 'archive.py', 'worker/runtime.py', 'worker/settings.py', 'worker/janitor.py'):
            write(archive, f'throngfission/execution/{name}', (Path(__file__).parent / name).read_bytes())
        for name in ('dirstree', 'cantok', 'printo', 'sigmatch', 'getsources', 'dill', 'pathspec', 'skelet', 'locklib', 'simtypes', 'denial', 'pristan', 'pyyaml', 'packaging', 'tomli', 'typing_extensions'):
            try:
                package = distribution(name)
            except PackageNotFoundError:
                if name not in ('tomli', 'typing_extensions'):
                    raise
                continue
            for file in package.files or []:
                if str(file).endswith('.py') or 'LICENSE' in str(file):
                    write(archive, str(file), Path(file.locate()).read_bytes())
    return base64.b64encode(buffer.getvalue()).decode('ascii')
