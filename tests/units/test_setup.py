import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from throngfission.errors import FissionError, IsolateClosedError
from throngfission.settings import Settings, SetupSettings
from throngfission.setup import Setup


def test_bundled_manifests_have_no_public_credentials_or_one_off_jobs():
    """The pinned installation data contains reusable Fission resources and no shared TLS keys."""
    path = Path(__file__).parents[2] / 'throngfission' / 'fission-v1.27.0.json'
    document = json.loads(path.read_text())
    assert document['_modification_notice'].startswith('Modified by throngfission from Fission v1.27.0')
    assert all(source.startswith('https://github.com/fission/fission/') for source in document['_upstream_sources'])
    bodies = document['resources']
    assert len([body for body in bodies if body['kind'] == 'CustomResourceDefinition']) == 9
    assert all(body['kind'] != 'Job' for body in bodies)
    secrets = [body for body in bodies if body['kind'] == 'Secret']
    assert secrets
    assert all(not value for body in secrets for value in body['data'].values())
    hooks = [body for body in bodies if body['kind'].endswith('WebhookConfiguration')]
    assert hooks
    assert all(not hook['clientConfig']['caBundle'] for body in hooks for hook in body['webhooks'])


@pytest.mark.parametrize('changes', [{'name': 'ambiguous'}, {'python_name': 'same', 'debian_name': 'same'}])
def test_ambiguous_function_names_fail_before_connecting(changes, monkeypatch):
    """A setup cannot collapse multiple runtime presets into the same function."""
    client = Mock()
    monkeypatch.setattr('throngfission.setup.Kubernetes', client)
    with pytest.raises(ValueError, match=r'unset|different'):
        Setup(Settings(**changes))
    client.assert_not_called()


@pytest.mark.parametrize(('version', 'changed_policy'), [('35', False), ('36', False), ('36+', True)])
def test_setup_orders_prerequisites_and_always_closes_client(version, changed_policy, monkeypatch):
    """Diagnostics follow provisioning and verified admission; every exit closes the API client."""
    api = Mock(request=Mock(return_value={'major': '1', 'minor': version}))
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock(return_value=api))
    provision = Mock(nodes=Mock(return_value=['node']), apparmor=Mock(return_value=''))
    monkeypatch.setattr('throngfission.setup.Provision', Mock(return_value=provision))
    setup = Setup(Settings())
    setup.fission = Mock()
    setup.admission = Mock()
    setup.function = Mock()
    setup.manifests.ensure = Mock(side_effect=lambda body: ({**body, **({'spec': {}} if changed_policy else {})}, False))
    if version == '35' or changed_policy:
        with pytest.raises(FissionError, match=r'1\.36|different settings'):
            setup.run()
        setup.function.assert_not_called()
    else:
        setup.run()
        setup.fission.assert_called_once_with()
        provision.images.assert_called_once_with(['node'])
        setup.admission.assert_called_once_with()
        assert [call.args for call in setup.function.call_args_list] == [('python',), ('debian',), ('ubuntu',)]
    api.close.assert_called_once_with()


def test_external_fission_installation_is_left_untouched(monkeypatch):
    """An existing external Fission installation is detected without creating release resources."""
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock())
    setup = Setup(Settings())
    setup.manifests = Mock(get=Mock(side_effect=[None, {'kind': 'Deployment'}]))
    setup.fission()
    setup.manifests.ensure.assert_not_called()


@pytest.mark.parametrize('marker', [None, {'data': {'version': '1.27.0'}}])
def test_fission_installation_resumes_each_manifest_with_local_credentials(marker, monkeypatch):
    """Fresh and interrupted installations reconcile each object and wait for CRDs and deployments."""
    bodies = [
        {'kind': 'CustomResourceDefinition', 'metadata': {'name': 'functions'}},
        {'kind': 'Secret', 'metadata': {'name': 'fission-internal-auth'}},
        {'kind': 'ValidatingWebhookConfiguration', 'metadata': {'name': 'webhook'}, 'webhooks': [{'clientConfig': {'caBundle': ''}}]},
        {'kind': 'Deployment', 'metadata': {'name': 'router', 'namespace': '__CONTROL_NAMESPACE__'}, 'target': '__FUNCTION_NAMESPACE__'},
    ]
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock())
    monkeypatch.setattr(Path, 'read_text', lambda _: json.dumps({'resources': bodies}))
    setup = Setup(Settings(namespace='workers', router_namespace='control'))
    setup.manifests = Mock(get=Mock(side_effect=[marker, None]))
    setup.credentials = Mock(return_value={'fission-internal-auth': {'secret': 'private'}, 'fission-webhook-certs': {'ca.crt': 'local-ca'}})
    setup.fission()
    submitted = [call.args[0] for call in setup.manifests.ensure.call_args_list]
    assert {body['metadata']['name'] for body in submitted[:2]} == {'control', 'workers'}
    assert submitted[4]['data'] == {'secret': 'private'}
    assert submitted[5]['webhooks'][0]['clientConfig']['caBundle'] == 'local-ca'
    assert submitted[-1]['metadata']['namespace'] == 'control'
    assert submitted[-1]['target'] == 'workers'
    assert [call.args[1] for call in setup.manifests.wait.call_args_list] == ['Established', 'Available']


@pytest.mark.parametrize(('auth', 'backup', 'cert'), [(None, None, None), (None, {'data': {'secret': 'existing'}}, None), ({'data': {'secret': 'existing'}}, None, {'data': {'ca.crt': 'saved'}})])
def test_installation_credentials_survive_partial_setup(auth, backup, cert, monkeypatch):
    """Existing secrets in either namespace win; only absent credentials are generated."""
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock())
    generate = Mock(return_value={'ca.crt': 'generated'})
    monkeypatch.setattr('throngfission.setup.certificate', generate)
    setup = Setup(Settings())
    setup.manifests.get = Mock(side_effect=([auth] if auth is not None else [None, backup]) + [cert])
    setup.manifests.ensure = Mock(side_effect=lambda body: (body, True))
    result = setup.credentials()
    if auth is None and backup is None:
        token = base64.b64decode(result['fission-internal-auth']['secret'])
        assert len(token) == 43
        assert all(character in b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for character in token)
    else:
        assert result['fission-internal-auth'] == {'secret': 'existing'}
    assert result['fission-webhook-certs'] == {'ca.crt': 'saved' if cert else 'generated'}
    assert generate.call_count == (0 if cert else 1)


def test_concurrent_secret_creation_uses_the_winning_credentials(monkeypatch):
    """A competing setup's persisted secrets supply every replica and webhook CA bundle."""
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock())
    monkeypatch.setattr('throngfission.setup.certificate', Mock(return_value={'ca.crt': 'candidate'}))
    setup = Setup(Settings())
    setup.manifests.get = Mock(return_value=None)
    setup.manifests.ensure = Mock(side_effect=[({'data': {'secret': 'winning-token'}}, False), ({'data': {'ca.crt': 'winning-certificate'}}, False)])
    assert setup.credentials() == {'fission-internal-auth': {'secret': 'winning-token'}, 'fission-webhook-certs': {'ca.crt': 'winning-certificate'}}


def test_admission_waits_for_actual_uid_remapping(monkeypatch):
    """Policy creation alone is insufficient: a dry-run must demonstrate host UID remapping."""
    api = Mock(request=Mock(side_effect=[{'spec': {'hostUsers': True}}, {'spec': {'hostUsers': False}}]))
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock(return_value=api))
    sleeper = Mock()
    monkeypatch.setattr('throngfission.setup.sleep', sleeper)
    Setup(Settings()).admission()
    sleeper.assert_called_once_with(1.0)
    assert api.request.call_count == 2
    assert all(call.kwargs == {'query': [('dryRun', 'All')]} for call in api.request.call_args_list)


@pytest.mark.parametrize('outcome', ['ready', 'connection-lost', 'root-failed', 'wrong-output', 'pip-failed', 'already-diagnosed'])
def test_diagnostic_checkpoint_requires_execution_and_cleanup(outcome, monkeypatch, tmp_path):
    """A diagnostic is recorded only after root, shell, pip and isolate deletion all succeed."""
    digest = hashlib.sha256(json.dumps(['resource'] * 10).encode()).hexdigest()
    api = Mock(request=Mock(side_effect=[{'metadata': {'annotations': {'throngfission.io/diagnostic': digest} if outcome == 'already-diagnosed' else {}}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)], {}]))
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock(return_value=api))
    isolate = Mock(run=Mock(return_value=Mock(success=outcome != 'root-failed', stdout='wrong' if outcome == 'wrong-output' else 'throngfission-ready\n', stderr='error')), chain=Mock(return_value=[Mock(success=True), Mock(success=outcome != 'pip-failed', stderr='pip-error')]))
    manager = Mock(get=Mock(side_effect=[IsolateClosedError('lost'), isolate] if outcome == 'connection-lost' else None, return_value=isolate))
    manager.cluster.core = '/api/v1/namespaces/fission-function/'
    factory = Mock(return_value=manager)
    monkeypatch.setattr('throngfission.setup.FissionManager', factory)
    sleeper = Mock()
    monkeypatch.setattr('throngfission.setup.sleep', sleeper)
    setup = Setup(Settings(), SetupSettings(work_dir=str(tmp_path)))
    if outcome in ['root-failed', 'wrong-output', 'pip-failed']:
        with pytest.raises(FissionError, match='diagnostic failed'):
            setup.function('ubuntu')
    else:
        setup.function('ubuntu')
    assert factory.call_args.kwargs['settings'].image == 'ubuntu'
    assert isolate.kill.call_count == (0 if outcome == 'already-diagnosed' else 1)
    assert len([call for call in api.request.call_args_list if call.args[0] == 'PATCH']) == (1 if outcome in ['ready', 'connection-lost'] else 0)
    assert sleeper.call_count == (1 if outcome == 'connection-lost' else 0)
    assert isolate.run.call_count == (0 if outcome == 'already-diagnosed' else 1)
    manager.cluster.api.close.assert_called_once()
    assert list(tmp_path.iterdir()) == []


def test_failed_diagnostic_cleanup_cannot_leave_a_success_marker(monkeypatch, tmp_path):
    """An unconfirmed isolate deletion leaves the diagnostic pending for the next setup."""
    api = Mock(request=Mock(side_effect=[{'metadata': {}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)]]))
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock(return_value=api))
    isolate = Mock(run=Mock(return_value=Mock(success=True, stdout='throngfission-ready\n')), chain=Mock(return_value=[]), kill=Mock(side_effect=FissionError('cleanup')))
    manager = Mock(get=Mock(return_value=isolate))
    manager.cluster.core = '/core/'
    monkeypatch.setattr('throngfission.setup.FissionManager', Mock(return_value=manager))
    with pytest.raises(FissionError, match='cleanup'):
        Setup(Settings(), SetupSettings(work_dir=str(tmp_path))).function('python')
    assert all(call.args[0] == 'GET' for call in api.request.call_args_list)
    manager.cluster.api.close.assert_called_once()


def test_diagnostic_connection_retries_share_one_deadline(monkeypatch, tmp_path):
    """Repeated acquisition failures stop at the original deadline without recording success."""
    api = Mock(request=Mock(side_effect=[{'metadata': {}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)]]))
    monkeypatch.setattr('throngfission.setup.Kubernetes', Mock(return_value=api))
    manager = Mock(get=Mock(side_effect=IsolateClosedError('disconnected')))
    manager.cluster.core = '/core/'
    monkeypatch.setattr('throngfission.setup.FissionManager', Mock(return_value=manager))
    deadline = Mock(check=Mock(side_effect=[None, TimeoutError('diagnostic deadline')]))
    timer = Mock(return_value=deadline)
    monkeypatch.setattr('throngfission.setup.TimeoutToken', timer)
    monkeypatch.setattr('throngfission.setup.sleep', Mock())
    with pytest.raises(TimeoutError, match='diagnostic deadline'):
        Setup(Settings(), SetupSettings(work_dir=str(tmp_path), diagnostic_timeout=123.0)).function('python')
    timer.assert_called_once_with(123.0)
    manager.get.assert_called_once_with(manager.read.return_value, deadline)
    assert all(call.args[0] == 'GET' for call in api.request.call_args_list)
    manager.cluster.api.close.assert_called_once()
