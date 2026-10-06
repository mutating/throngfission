import tarfile
from io import BytesIO
from unittest.mock import Mock

import pytest
from throng.extensions.temporary_directory.read import read_directory as original_read

from throngfission.archive import read_directory, restore_directory


def test_snapshot_matches_builtin(tmp_path):
    """Snapshots preserve throng's TAR format and dirstree exclusion rules."""
    (tmp_path / 'keep.txt').write_text('persistent')
    (tmp_path / 'ignore.txt').write_text('excluded')
    state = read_directory(tmp_path, ['ignore.txt'])
    assert state == original_read(tmp_path, ['ignore.txt'])
    target = tmp_path / 'restored'
    target.mkdir()
    restore_directory(state, target)
    assert (target / 'keep.txt').read_text() == 'persistent'
    assert not (target / 'ignore.txt').exists()


@pytest.mark.parametrize(('name', 'kind', 'link'), [
    ('../outside', tarfile.REGTYPE, ''), ('file', tarfile.SYMTYPE, '../outside'),
    ('file', tarfile.LNKTYPE, '../outside'), ('device', tarfile.CHRTYPE, ''),
])
def test_reject_escaping_archive(tmp_path, name, kind, link):
    """Archive paths, links and devices cannot write outside the workspace."""
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, link
        archive.addfile(member)
    with pytest.raises(ValueError, match=r'outside|subpath|Only files'):
        restore_directory(buffer.getvalue(), tmp_path)


@pytest.mark.parametrize('kind', [tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_accept_internal_entries(tmp_path, monkeypatch, kind):
    """Directory entries and links confined to the workspace pass validation."""
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        member = tarfile.TarInfo('entry')
        member.type, member.linkname = kind, 'target'
        archive.addfile(member)
    extract = Mock()
    monkeypatch.setattr(tarfile.TarFile, 'extract', extract)
    restore_directory(buffer.getvalue(), tmp_path)
    assert extract.call_args.args[0].name == 'entry'
