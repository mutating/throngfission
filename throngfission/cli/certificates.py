"""Generate a private, cluster-specific webhook certificate using OpenSSL."""

import base64
from pathlib import Path
from subprocess import DEVNULL, run
from tempfile import TemporaryDirectory
from typing import Dict

from throngfission.cli.settings import SetupSettings


def certificate(namespace: str, options: SetupSettings) -> Dict[str, str]:
    with TemporaryDirectory(prefix='throngfission-tls-', dir=options.work_dir) as directory:
        path = Path(directory)
        configuration = path / 'openssl.cnf'
        configuration.write_text(
            '[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=extensions\n'
            f'[subject]\nCN=webhook-service.{namespace}.svc\n'
            '[extensions]\nbasicConstraints=critical,CA:TRUE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign\n'
            'extendedKeyUsage=serverAuth\n'
            f'subjectAltName=DNS:webhook-service.{namespace}.svc,DNS:webhook-service.{namespace}.svc.cluster.local\n',
        )
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', str(options.certificate_days), '-config', str(configuration), '-keyout', str(path / 'key.pem'), '-out', str(path / 'cert.pem')], check=True, stdout=DEVNULL, stderr=DEVNULL, timeout=options.setup_timeout)
        cert = base64.b64encode((path / 'cert.pem').read_bytes()).decode()
        return {'ca.crt': cert, 'tls.crt': cert, 'tls.key': base64.b64encode((path / 'key.pem').read_bytes()).decode()}
