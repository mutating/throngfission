import base64
from pathlib import Path
from subprocess import CalledProcessError
from unittest.mock import Mock

import pytest

from throngfission.cli.certificates import certificate
from throngfission.cli.settings import SetupSettings


def test_certificate_uses_private_temporary_files_and_configured_names(tmp_path, monkeypatch):
    """OpenSSL receives the namespace and lifetime while temporary key material is removed."""
    inputs = []
    process = Mock(side_effect=lambda args, **_: (inputs.append(Path(args[args.index('-config') + 1]).read_text()), Path(args[-3]).write_bytes(b'key'), Path(args[-1]).write_bytes(b'certificate')))
    monkeypatch.setattr('throngfission.cli.certificates.run', process)
    result = certificate('control', SetupSettings(work_dir=str(tmp_path), certificate_days=123, setup_timeout=7.0))
    assert {key: base64.b64decode(value) for key, value in result.items()} == {'ca.crt': b'certificate', 'tls.crt': b'certificate', 'tls.key': b'key'}
    assert inputs == [
        '[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n'
        '[subject]\nCN=webhook-service.control.svc\n'
        '[extensions]\nbasicConstraints=critical,CA:TRUE\n'
        'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
        'extendedKeyUsage=serverAuth\n'
        'subjectAltName=DNS:webhook-service.control.svc,DNS:webhook-service.control.svc.cluster.local\n',
    ]
    assert process.call_args.args[0][process.call_args.args[0].index('-days') + 1] == '123'
    assert process.call_args.kwargs['timeout'] == 7.0
    assert list(tmp_path.iterdir()) == []


def test_failed_certificate_generation_removes_temporary_files(tmp_path, monkeypatch):
    """A failed OpenSSL invocation leaves no private installation files behind."""
    monkeypatch.setattr('throngfission.cli.certificates.run', Mock(side_effect=CalledProcessError(1, 'openssl')))
    with pytest.raises(CalledProcessError):
        certificate('fission', SetupSettings(work_dir=str(tmp_path)))
    assert list(tmp_path.iterdir()) == []
