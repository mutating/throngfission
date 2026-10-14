import base64
from subprocess import run

import pytest

from throngfission.execution.api import Kubernetes


def test_webhook_uses_its_cluster_certificate(setup_cluster, tmp_path):
    """The webhook trusts the generated certificate and its service name matches the TLS identity."""
    if setup_cluster is None:
        pytest.skip('Certificate creation is checked in the disposable installation.')
    settings, _, _ = setup_cluster
    api = Kubernetes(settings)
    try:
        secret = api.request('GET', '/api/v1/namespaces/fission/secrets/fission-webhook-certs')['data']
        authentication = api.request('GET', '/api/v1/namespaces/fission/secrets/fission-internal-auth')['data']['secret']
        token = base64.b64decode(authentication)
        assert len(token) == 43
        assert all(character in b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for character in token)
        certificate = tmp_path / 'certificate.pem'
        certificate.write_bytes(base64.b64decode(secret['tls.crt']))
        result = run(['openssl', 'verify', '-CAfile', str(certificate), '-verify_hostname', 'webhook-service.fission.svc', str(certificate)], check=False, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        webhook = api.request('GET', '/apis/admissionregistration.k8s.io/v1/validatingwebhookconfigurations/fission-validating-webhooks')
        assert all(item['clientConfig']['caBundle'] == secret['ca.crt'] for item in webhook['webhooks'])
    finally:
        api.close()
