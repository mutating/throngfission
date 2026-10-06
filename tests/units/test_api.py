import json
import ssl
from unittest.mock import Mock

import pytest
from kubernetes.client.rest import ApiException  # type: ignore[import-untyped]
from urllib3.exceptions import MaxRetryError

from throngfission.api import Kubernetes
from throngfission.errors import APIError
from throngfission.settings import Settings


@pytest.mark.parametrize('method', ['GET', 'PATCH'])
@pytest.mark.parametrize(('kubeconfig', 'context'), [('config', 'compute'), ('', '')])
def test_official_client_requests_release_connections(monkeypatch, method, kubeconfig, context):
    """SDK calls use bounded requests and release their HTTP connection."""
    response = Mock(read=Mock(return_value=b'{"items":[]}'))
    client = Mock(call_api=Mock(return_value=response))
    factory = Mock(return_value=client)
    monkeypatch.setattr('kubernetes.config.new_client_from_config', factory)
    api = Kubernetes(Settings(kubeconfig=kubeconfig, context=context))
    assert api.request(method, '/pods') == {'items': []}
    factory.assert_called_once_with(kubeconfig or None, context or None)
    options = client.call_api.call_args.kwargs
    assert options['_request_timeout'] == 30.0
    assert options['header_params']['Content-Type'] == ('application/merge-patch+json' if method == 'PATCH' else 'application/json')
    response.release_conn.assert_called_once()
    api.close()
    client.close.assert_called_once()


def test_sdk_failure_preserves_status(monkeypatch):
    """Kubernetes status codes remain available for capacity and cleanup logic."""
    monkeypatch.setattr('kubernetes.config.new_client_from_config', Mock(return_value=Mock(call_api=Mock(side_effect=ApiException(status=409, reason='conflict')))))
    with pytest.raises(APIError, match='conflict') as caught:
        Kubernetes(Settings()).request('POST', '/pods', {})
    assert caught.value.status == 409


@pytest.mark.parametrize(('method', 'failure'), [
    ('GET', MaxRetryError(None, '/pods', 'connection refused')),
    ('DELETE', ApiException(status=503, reason='API restarting')),
])
def test_safe_requests_survive_a_control_plane_restart(monkeypatch, method, failure):
    """A brief API outage cannot abandon a pod check or UID-guarded cleanup."""
    response = Mock(read=Mock(return_value=b'{"items":[]}'))
    client = Mock(call_api=Mock(side_effect=[failure, response]))
    monkeypatch.setattr('kubernetes.config.new_client_from_config', Mock(return_value=client))
    pause = Mock()
    monkeypatch.setattr('throngfission.api.sleep', pause)
    assert Kubernetes(Settings()).request(method, '/pods') == {'items': []}
    assert client.call_api.call_count == 2
    pause.assert_called_once_with(0.1)
    response.release_conn.assert_called_once()


@pytest.mark.parametrize(('method', 'failure', 'expected'), [
    ('GET', MaxRetryError(None, '/pods', 'offline'), MaxRetryError),
    ('GET', ApiException(status=503, reason='unavailable'), APIError),
    ('POST', MaxRetryError(None, '/pods', 'offline'), MaxRetryError),
    ('GET', ApiException(status=403, reason='denied'), APIError),
])
def test_api_recovery_is_bounded_and_preserves_permanent_errors(monkeypatch, method, failure, expected):
    """Prolonged outages and unsafe writes fail, while denial remains immediate."""
    client = Mock(call_api=Mock(side_effect=failure))
    monkeypatch.setattr('kubernetes.config.new_client_from_config', Mock(return_value=client))
    monkeypatch.setattr('throngfission.api.monotonic', Mock(side_effect=[0, 2]))
    pause = Mock()
    monkeypatch.setattr('throngfission.api.sleep', pause)
    with pytest.raises(expected):
        Kubernetes(Settings(api_recovery_timeout=1.0)).request(method, '/pods')
    client.call_api.assert_called_once()
    pause.assert_not_called()


def test_bad_json_still_releases_connection(monkeypatch):
    """Malformed API responses cannot leak pooled HTTP connections."""
    response = Mock(read=Mock(return_value=b'bad'))
    monkeypatch.setattr('kubernetes.config.new_client_from_config', Mock(return_value=Mock(call_api=Mock(return_value=response))))
    with pytest.raises(json.JSONDecodeError):
        Kubernetes(Settings()).request('GET', '/pods')
    response.release_conn.assert_called_once()


@pytest.mark.parametrize('verify', [True, False])
def test_websocket_reuses_kubeconfig_credentials(monkeypatch, verify):
    """Proxy sockets preserve kubeconfig certificate and authentication choices."""
    client = Mock()
    client.configuration = Mock(host='https://cluster', verify_ssl=verify, ssl_ca_cert='ca', cert_file='cert', key_file='key')
    client.update_params_for_auth.side_effect = lambda headers, *_: headers.update({'Authorization': 'Bearer credential'})
    monkeypatch.setattr('kubernetes.config.new_client_from_config', Mock(return_value=client))
    url, headers, options = Kubernetes(Settings()).connection()
    assert url == 'wss://cluster'
    assert headers == {'Authorization': 'Bearer credential'}
    assert options == {'cert_reqs': ssl.CERT_REQUIRED if verify else ssl.CERT_NONE, 'ca_certs': 'ca', 'certfile': 'cert', 'keyfile': 'key'}
