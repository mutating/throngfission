from hashlib import sha256
from io import BytesIO
from tarfile import open as open_tar

from cantok import TimeoutToken


def test_snapshot_roundtrip_between_independent_isolates(manager):
    """A TAR snapshot transfers workspace changes without sharing pod state."""
    (manager.path / 'initial').write_bytes(b'\x00\xff')
    first = manager.get(manager.read(), TimeoutToken(180))
    try:
        assert first.run('sh -c "printf saved > created"').success
        snapshot = first.read()
        first_pod = first.pod
    finally:
        first.kill()
    second = manager.get(snapshot, TimeoutToken(180))
    try:
        assert second.pod != first_pod
        with open_tar(fileobj=BytesIO(second.read())) as archive:
            assert archive.extractfile('initial').read() == b'\x00\xff'
            assert archive.extractfile('created').read() == b'saved'
    finally:
        second.kill()


def test_large_snapshot_roundtrip(manager):
    """An 80 MiB workspace crosses Fission's message limit in both directions intact."""
    block = bytes(range(256)) * 4096
    digest = sha256()
    with (manager.path / 'large.bin').open('wb') as stream:
        for _ in range(80):
            stream.write(block)
            digest.update(block)
        stream.write(b'end')
        digest.update(b'end')
    isolate = manager.get(manager.read(), TimeoutToken(300))
    try:
        result = isolate.run('python -c "import hashlib; print(hashlib.sha256(open(\'large.bin\', \'rb\').read()).hexdigest())"')
        assert result.success
        assert result.stdout.strip() == digest.hexdigest()
        with open_tar(fileobj=BytesIO(isolate.read())) as archive:
            member = archive.getmember('large.bin')
            assert member.size == 80 * 1024 * 1024 + 3
            received = sha256()
            with archive.extractfile(member) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    received.update(chunk)
        assert received.digest() == digest.digest()
        assert isolate.run('rm large.bin').success
        with open_tar(fileobj=BytesIO(isolate.read())) as archive:
            assert archive.getnames() == []
    finally:
        isolate.kill()
