import base64
import hashlib
import json
from inspect import getfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from throng.errors import InterruptedChainError, NotSuccessfulRunError

from throngfission.cli.settings import SetupSettings
from throngfission.cli.setup import Setup, reconfigure
from throngfission.execution.cluster import Cluster
from throngfission.execution.errors import APIError, FissionError, IsolateClosedError
from throngfission.execution.isolate import RunResult
from throngfission.execution.settings import Settings


class CustomFormattedText(str):
    __slots__ = ()

    def __format__(self, _format_spec: str) -> str:
        return 'unexpected custom formatting'


def test_bundled_manifests_have_no_public_credentials_or_one_off_jobs():
    """The pinned installation data contains reusable Fission resources and no shared TLS keys."""
    path = Path(getfile(Setup)).with_name('assets') / 'fission-v1.27.0.json'
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
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', client)
    with pytest.raises(ValueError, match=r'unset|different'):
        Setup(Settings(**changes))
    client.assert_not_called()


@pytest.mark.parametrize(('version', 'changed_policy'), [('35', False), ('36', False), ('36+', True)])
def test_setup_orders_prerequisites_and_always_closes_client(version, changed_policy, monkeypatch):
    """Diagnostics follow provisioning and verified admission; every exit closes the API client."""
    api = Mock(request=Mock(return_value={'major': '1', 'minor': version}))
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=api))
    provision = Mock(nodes=Mock(return_value=['node']), apparmor=Mock(return_value=''))
    monkeypatch.setattr('throngfission.cli.setup.Provision', Mock(return_value=provision))
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
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock())
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
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock())
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
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock())
    generate = Mock(return_value={'ca.crt': 'generated'})
    monkeypatch.setattr('throngfission.cli.setup.certificate', generate)
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
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock())
    monkeypatch.setattr('throngfission.cli.setup.certificate', Mock(return_value={'ca.crt': 'candidate'}))
    setup = Setup(Settings())
    setup.manifests.get = Mock(return_value=None)
    setup.manifests.ensure = Mock(side_effect=[({'data': {'secret': 'winning-token'}}, False), ({'data': {'ca.crt': 'winning-certificate'}}, False)])
    assert setup.credentials() == {'fission-internal-auth': {'secret': 'winning-token'}, 'fission-webhook-certs': {'ca.crt': 'winning-certificate'}}


def test_admission_waits_for_actual_uid_remapping(monkeypatch):
    """Policy creation alone is insufficient: a dry-run must demonstrate host UID remapping."""
    api = Mock(request=Mock(side_effect=[{'spec': {'hostUsers': True}}, {'spec': {'hostUsers': False}}]))
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=api))
    sleeper = Mock()
    monkeypatch.setattr('throngfission.cli.setup.sleep', sleeper)
    Setup(Settings()).admission()
    sleeper.assert_called_once_with(1.0)
    assert api.request.call_count == 2
    assert all(call.kwargs == {'query': [('dryRun', 'All')]} for call in api.request.call_args_list)


@pytest.mark.parametrize('outcome', ['ready', 'connection-lost', 'root-failed', 'wrong-output', 'pip-failed', 'cancelled', 'already-diagnosed'])
def test_diagnostic_checkpoint_requires_execution_and_cleanup(outcome, monkeypatch, tmp_path, capsys):
    """A diagnostic is recorded only after root, shell, pip and isolate deletion all succeed."""
    configure = Mock()
    monkeypatch.setattr('throngfission.cli.setup.reconfigure', configure)
    digest = hashlib.sha256(json.dumps(['resource'] * 10).encode()).hexdigest()
    api = Mock(request=Mock(side_effect=[{'metadata': {'annotations': {'throngfission.io/diagnostic': digest} if outcome == 'already-diagnosed' else {}}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)], {}]))
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=api))
    failure = NotSuccessfulRunError('pip failed', RunResult(success=False, returncode=7, stdout='pip-out', stderr='pip-error')) if outcome == 'pip-failed' else InterruptedChainError('cancelled') if outcome == 'cancelled' else None
    isolate = Mock(run=Mock(return_value=Mock(success=outcome != 'root-failed', stdout='wrong' if outcome == 'wrong-output' else 'throngfission-ready\n', stderr='error')), chain=Mock(side_effect=failure))
    manager = Mock(get=Mock(side_effect=[IsolateClosedError('lost'), isolate] if outcome == 'connection-lost' else None, return_value=isolate))
    manager.cluster.core = '/api/v1/namespaces/fission-function/'
    factory = Mock(return_value=manager)
    monkeypatch.setattr('throngfission.cli.setup.FissionManager', factory)
    sleeper = Mock()
    monkeypatch.setattr('throngfission.cli.setup.sleep', sleeper)
    setup = Setup(Settings(), SetupSettings(work_dir=str(tmp_path)))
    if outcome in ['root-failed', 'wrong-output', 'pip-failed', 'cancelled']:
        with pytest.raises(FissionError, match='diagnostic') as caught:
            setup.function('ubuntu')
        assert str(caught.value) == {
            'root-failed': 'Runtime diagnostic failed for ubuntu: errorthrongfission-ready\n',
            'wrong-output': 'Runtime diagnostic failed for ubuntu: errorwrong',
            'pip-failed': 'Shell/pip diagnostic failed for ubuntu: pip-errorpip-out',
            'cancelled': 'Shell/pip diagnostic cancelled for ubuntu.',
        }[outcome]
        if failure is not None:
            assert caught.value.__cause__ is failure
        if outcome == 'pip-failed':
            assert 'pip-error' in str(caught.value)
            assert 'pip-out' in str(caught.value)
    else:
        setup.function('ubuntu')
    captured = capsys.readouterr()
    if outcome == 'already-diagnosed':
        assert captured.out == 'Function already prepared: throngfission-ubuntu\n'
    elif outcome == 'connection-lost':
        assert captured.out == (
            'Diagnosing ubuntu as throngfission-ubuntu\n'
            'Diagnostic connection lost before commands; waiting for a new worker.\n'
        )
    else:
        assert captured.out == 'Diagnosing ubuntu as throngfission-ubuntu\n'
    assert captured.err == ''
    assert factory.call_args.kwargs['settings'].image == 'ubuntu'
    configure.assert_called_once_with(manager.cluster)
    assert isolate.kill.call_count == (0 if outcome == 'already-diagnosed' else 1)
    assert len([call for call in api.request.call_args_list if call.args[0] == 'PATCH']) == (1 if outcome in ['ready', 'connection-lost'] else 0)
    assert sleeper.call_count == (1 if outcome == 'connection-lost' else 0)
    assert isolate.run.call_count == (0 if outcome == 'already-diagnosed' else 1)
    if isolate.chain.called:
        assert isolate.chain.call_args.kwargs['exception'] is True
    manager.cluster.api.close.assert_called_once()
    assert list(tmp_path.iterdir()) == []


def test_failed_diagnostic_cleanup_cannot_leave_a_success_marker(monkeypatch, tmp_path):
    """An unconfirmed isolate deletion leaves the diagnostic pending for the next setup."""
    monkeypatch.setattr('throngfission.cli.setup.reconfigure', Mock())
    api = Mock(request=Mock(side_effect=[{'metadata': {}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)]]))
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=api))
    isolate = Mock(run=Mock(return_value=Mock(success=True, stdout='throngfission-ready\n')), chain=Mock(return_value=[]), kill=Mock(side_effect=FissionError('cleanup')))
    manager = Mock(get=Mock(return_value=isolate))
    manager.cluster.core = '/core/'
    monkeypatch.setattr('throngfission.cli.setup.FissionManager', Mock(return_value=manager))
    with pytest.raises(FissionError, match='cleanup'):
        Setup(Settings(), SetupSettings(work_dir=str(tmp_path))).function('python')
    assert all(call.args[0] == 'GET' for call in api.request.call_args_list)
    manager.cluster.api.close.assert_called_once()


@pytest.mark.parametrize(('stderr', 'stdout', 'details'), [
    ('error', 'output', 'erroroutput'),
    ('error', '', 'error'),
    ('', 'output', 'output'),
    ('', '', ''),
    (None, None, 'NoneNone'),
    ('ошибка: "{value}"\n\tC:\\work\n', 'вывод 🙂\nстрока 2\n', 'ошибка: "{value}"\n\tC:\\work\nвывод 🙂\nстрока 2\n'),  # noqa: RUF001
    (CustomFormattedText('error'), CustomFormattedText('output'), 'erroroutput'),
], ids=['both', 'stderr-only', 'stdout-only', 'empty', 'none', 'unicode-and-escapes', 'str-conversion'])
@pytest.mark.parametrize('preset', ['python', 'debian', 'ubuntu'])
@pytest.mark.parametrize('stage', ['runtime', 'pip'])
def test_diagnostic_errors_preserve_complete_output(stage, preset, stderr, stdout, details, monkeypatch, tmp_path, capsys):  # noqa: PLR0913
    """Failures retain their exact prefix, stderr-before-stdout ordering and str conversion."""
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=Mock(request=Mock(return_value={'metadata': {'uid': 'resource'}}))))
    monkeypatch.setattr('throngfission.cli.setup.reconfigure', Mock())
    result = RunResult(success=False, returncode=7, stderr=stderr, stdout=stdout)
    failure = NotSuccessfulRunError('failed', result)
    isolate = Mock(run=Mock(return_value=result if stage == 'runtime' else RunResult(success=True, stdout='throngfission-ready\n')), chain=Mock(side_effect=failure))
    manager = Mock(get=Mock(return_value=isolate))
    manager.cluster.core = '/core/'
    monkeypatch.setattr('throngfission.cli.setup.FissionManager', Mock(return_value=manager))
    with pytest.raises(FissionError) as caught:
        Setup(Settings(), SetupSettings(work_dir=str(tmp_path))).function(preset)
    prefixes = {
        ('runtime', 'python'): 'Runtime diagnostic failed for python: ',
        ('runtime', 'debian'): 'Runtime diagnostic failed for debian: ',
        ('runtime', 'ubuntu'): 'Runtime diagnostic failed for ubuntu: ',
        ('pip', 'python'): 'Shell/pip diagnostic failed for python: ',
        ('pip', 'debian'): 'Shell/pip diagnostic failed for debian: ',
        ('pip', 'ubuntu'): 'Shell/pip diagnostic failed for ubuntu: ',
    }
    assert str(caught.value) == f'{prefixes[stage, preset]}{details}'
    assert caught.value.__cause__ is (failure if stage == 'pip' else None)
    assert capsys.readouterr().err == ''
    isolate.kill.assert_called_once_with()


def test_diagnostic_connection_retries_share_one_deadline(monkeypatch, tmp_path):
    """Repeated acquisition failures stop at the original deadline without recording success."""
    monkeypatch.setattr('throngfission.cli.setup.reconfigure', Mock())
    api = Mock(request=Mock(side_effect=[{'metadata': {}}, *[{'metadata': {'uid': 'resource'}} for _ in range(10)]]))
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock(return_value=api))
    manager = Mock(get=Mock(side_effect=IsolateClosedError('disconnected')))
    manager.cluster.core = '/core/'
    monkeypatch.setattr('throngfission.cli.setup.FissionManager', Mock(return_value=manager))
    deadline = Mock(check=Mock(side_effect=[None, TimeoutError('diagnostic deadline')]))
    timer = Mock(return_value=deadline)
    monkeypatch.setattr('throngfission.cli.setup.TimeoutToken', timer)
    monkeypatch.setattr('throngfission.cli.setup.sleep', Mock())
    with pytest.raises(TimeoutError, match='diagnostic deadline'):
        Setup(Settings(), SetupSettings(work_dir=str(tmp_path), diagnostic_timeout=123.0)).function('python')
    timer.assert_called_once_with(123.0)
    manager.get.assert_called_once_with(manager.read.return_value, deadline)
    assert all(call.args[0] == 'GET' for call in api.request.call_args_list)
    manager.cluster.api.close.assert_called_once()


@pytest.mark.parametrize(('saved_mode', 'current_mode', 'active'), [
    ('none', 'soft', False), ('soft', 'hard', False), ('hard', 'none', False),
    ('none', 'soft', True), ('other-settings', 'soft', False), ('soft', 'soft', False),
])
def test_explicit_reconfiguration_changes_only_idle_scheduling(saved_mode, current_mode, active, monkeypatch):
    """Setup replaces an idle function only for a known mode change, never for unrelated drift."""
    settings = Settings(anti_affinity=current_mode)
    saved = Cluster.fingerprint(Settings(anti_affinity=saved_mode), 'archive') if saved_mode != 'other-settings' else 'unrelated'
    api = Mock(request=Mock(side_effect=[{'data': {'fingerprint': saved}}, {'items': [{'metadata': {'name': 'busy'}}] if active else []}]))
    monkeypatch.setattr('throngfission.execution.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cli.setup.worker_archive', Mock(return_value='archive'))
    cluster = Cluster(settings)
    uninstall = Mock()
    monkeypatch.setattr(cluster, 'uninstall', uninstall)
    if active:
        with pytest.raises(FissionError, match='active isolates'):
            reconfigure(cluster)
    else:
        reconfigure(cluster)
    assert uninstall.call_count == (1 if saved_mode not in ('other-settings', current_mode) and not active else 0)
    assert api.request.call_count == (2 if saved_mode not in ('other-settings', current_mode) else 1)
    if api.request.call_count == 2:
        assert api.request.call_args.kwargs == {'query': [('labelSelector', f'throngfission.io/installation={settings.function_name},managed=false')]}


@pytest.mark.parametrize('status', [404, 403])
def test_reconfiguration_requires_a_readable_owned_installation(status, monkeypatch):
    """A missing function is left for normal setup, while API failures surface."""
    api = Mock(request=Mock(side_effect=APIError(status, 'unavailable')))
    monkeypatch.setattr('throngfission.execution.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    if status == 404:
        reconfigure(cluster)
    else:
        with pytest.raises(APIError, match='unavailable'):
            reconfigure(cluster)
    api.request.assert_called_once_with('GET', f'{cluster.core}configmaps/{cluster.settings.function_name}')


def test_reconfiguration_failure_stops_setup_before_allocation(monkeypatch, tmp_path):
    """A refused scheduling change closes the client and leaves workers and checkpoints alone."""
    monkeypatch.setattr('throngfission.cli.setup.Kubernetes', Mock())
    manager = Mock()
    monkeypatch.setattr('throngfission.cli.setup.FissionManager', Mock(return_value=manager))
    configure = Mock(side_effect=FissionError('active isolates'))
    monkeypatch.setattr('throngfission.cli.setup.reconfigure', configure)
    setup = Setup(Settings(), SetupSettings(work_dir=str(tmp_path)))
    with pytest.raises(FissionError, match='active isolates'):
        setup.function('python')
    configure.assert_called_once_with(manager.cluster)
    manager.cluster.ensure.assert_not_called()
    manager.get.assert_not_called()
    setup.api.request.assert_not_called()
    manager.cluster.api.close.assert_called_once_with()
    assert list(tmp_path.iterdir()) == []
