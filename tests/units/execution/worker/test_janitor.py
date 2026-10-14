import json
from email.message import Message
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from throngfission.execution.worker.janitor import inspect_pod, main, reap, request
from throngfission.execution.worker.settings import WorkerSettings


@pytest.mark.parametrize(('method', 'body'), [('GET', None), ('PATCH', {'metadata': {}}), ('DELETE', {'preconditions': {}})])
def test_request_encodes_body_and_releases_response(monkeypatch, method, body):
    """Janitor requests select the API content type and close the response."""
    response = Mock(read=Mock(return_value=b'{"items":[]}'))
    manager = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    open_url = Mock(return_value=manager)
    monkeypatch.setattr('throngfission.execution.worker.janitor.urlopen', open_url)
    context = Mock()
    assert request('https://cluster/pods', {'Authorization': 'Bearer secret'}, method, body, context, timeout=7.5) == {'items': []}
    prepared = open_url.call_args.args[0]
    assert prepared.method == method
    assert prepared.data == (json.dumps(body).encode() if body is not None else None)
    assert prepared.get_header('Content-type') == ('application/merge-patch+json' if method == 'PATCH' else 'application/json')
    assert prepared.get_header('Authorization') == 'Bearer secret'
    assert open_url.call_args.kwargs == {'context': context, 'timeout': 7.5}
    manager.__exit__.assert_called_once()


@pytest.mark.parametrize('status', [None, 404, 409, 403])
def test_scan_uses_service_account_and_tolerates_deletion_races(monkeypatch, status):
    """Scanning stays installation-scoped and survives pods deleted concurrently."""
    monkeypatch.setattr('throngfission.execution.worker.janitor.Path.read_text', Mock(return_value='token\n'))
    context = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.ssl.create_default_context', Mock(return_value=context))
    query = Mock(return_value={'items': [{'metadata': {'name': 'one'}}, {'metadata': {'name': 'two'}}]})
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    inspect = Mock(side_effect=HTTPError('url', status, 'failed', {}, None) if status else None)
    monkeypatch.setattr('throngfission.execution.worker.janitor.inspect_pod', inspect)
    settings = WorkerSettings(namespace='functions', name='runner', secret='secret', janitor_request_timeout=7.5)
    if status == 403:
        with pytest.raises(HTTPError):
            reap(settings)
    else:
        reap(settings)
        assert inspect.call_count == 2
    assert query.call_args.args[0] == 'https://kubernetes.default.svc/api/v1/namespaces/functions/pods?labelSelector=throngfission.io/installation%3Drunner,managed%3Dfalse'
    assert query.call_args.args[1]['Authorization'] == 'Bearer token'
    assert inspect.call_args.args[3:] == (context, settings)
    assert query.call_args.kwargs == {'context': context, 'timeout': 7.5}


def test_terminating_pod_is_left_to_kubernetes(monkeypatch):
    """An existing deletion is never replaced with a forced deletion."""
    query = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    inspect_pod({'metadata': {'deletionTimestamp': 'now'}}, 'base', {}, Mock(), WorkerSettings(secret='secret'))
    query.assert_not_called()


@pytest.mark.parametrize(('health', 'delete'), [({'closed': False, 'age': 0}, False), ({'closed': False, 'age': 47.5}, False), ({'closed': False, 'age': 48}, True), ({'closed': True, 'age': None}, True)])
def test_only_closed_or_expired_sessions_are_deleted(monkeypatch, health, delete):
    """Healthy sessions survive; closed or expired sessions are deleted by UID."""
    query = Mock(return_value=health)
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    inspect_pod({'metadata': {'name': 'worker', 'uid': 'uid'}, 'status': {'podIP': '10.0.0.1'}}, 'base', {'Authorization': 'token'}, Mock(), WorkerSettings(secret='secret', session_timeout=47.5, janitor_request_timeout=7.5))
    assert query.call_args_list[0].args == ('http://10.0.0.1:8888/', {'X-Throngfission-Token': 'secret'})
    assert query.call_count == (2 if delete else 1)
    assert all(call.kwargs['timeout'] == 7.5 for call in query.call_args_list)
    if delete:
        assert query.call_args.args[0:3] == ('base/worker', {'Authorization': 'token'}, 'DELETE')
        assert query.call_args.args[3]['preconditions'] == {'uid': 'uid'}


@pytest.mark.parametrize('failure', [URLError('offline'), TimeoutError(), ValueError('bad JSON'), {'closed': False, 'age': None}, {}])
def test_unreachable_worker_gets_a_persistent_grace_period(monkeypatch, failure):
    """One failed probe records a timestamp instead of deleting a possibly live pod."""
    query = Mock(side_effect=[failure, {}])
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    monkeypatch.setattr('throngfission.execution.worker.janitor.time', lambda: 1000)
    inspect_pod({'metadata': {'name': 'worker'}, 'status': {'podIP': '10.0.0.1'}}, 'base', {}, Mock(), WorkerSettings(secret='secret'))
    assert query.call_args.args[2:4] == ('PATCH', {'metadata': {'annotations': {'throngfission.io/unreachable-since': '1000'}}})


@pytest.mark.parametrize(('since', 'delete'), [('855', False), ('854', True)])
def test_unreachable_grace_survives_janitor_restarts(monkeypatch, since, delete):
    """The persisted timestamp expires only after the configured unreachable grace."""
    query = Mock(return_value={})
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    monkeypatch.setattr('throngfission.execution.worker.janitor.time', lambda: 1000)
    inspect_pod({'metadata': {'name': 'worker', 'uid': 'uid', 'annotations': {'throngfission.io/unreachable-since': since}}}, 'base', {}, Mock(), WorkerSettings(secret='secret', janitor_unreachable_timeout=145.0))
    assert query.call_count == int(delete)
    if delete:
        assert query.call_args.args[2] == 'DELETE'


def test_recovered_session_clears_the_previous_failure(monkeypatch):
    """A healthy heartbeat resets the unreachable grace period."""
    query = Mock(return_value={'closed': False, 'age': 1})
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    inspect_pod({'metadata': {'name': 'worker', 'annotations': {'throngfission.io/unreachable-since': '1'}}, 'status': {'podIP': '10.0.0.1'}}, 'base', {}, Mock(), WorkerSettings(secret='secret'))
    assert query.call_args.args[2:4] == ('PATCH', {'metadata': {'annotations': {'throngfission.io/unreachable-since': None}}})


@pytest.mark.parametrize('status', [404, 409, 403])
def test_delete_races_do_not_mask_permission_errors(monkeypatch, status):
    """A vanished or replaced pod is safe; forbidden cleanup must remain visible."""
    query = Mock(side_effect=[{'closed': True}, HTTPError('url', status, 'failed', {}, None)])
    monkeypatch.setattr('throngfission.execution.worker.janitor.request', query)
    pod = {'metadata': {'name': 'worker', 'uid': 'uid'}, 'status': {'podIP': '10.0.0.1'}}
    if status == 403:
        with pytest.raises(HTTPError):
            inspect_pod(pod, 'base', {}, Mock(), WorkerSettings(secret='secret'))
    else:
        inspect_pod(pod, 'base', {}, Mock(), WorkerSettings(secret='secret'))


def test_entrypoint_reads_skelet_settings(monkeypatch):
    """The CronJob uses the same typed environment settings as the worker."""
    settings = WorkerSettings(namespace='ns', name='runner', secret='secret', session_timeout=30.0)
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=settings))
    cleanup = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.reap', cleanup)
    main()
    cleanup.assert_called_once_with(settings)


@pytest.mark.parametrize('failure', [
    TimeoutError('API stalled'), URLError('API offline'), ConnectionResetError('API reset'),
    HTTPError('url', 503, 'restarting', Message(), None),
])
@pytest.mark.parametrize('attempts', [1, 2])
def test_janitor_retries_a_transient_control_plane_failure(monkeypatch, failure, attempts):
    """Brief repeated API outages restart the scan without losing its job."""
    settings = WorkerSettings(secret='secret', api_recovery_timeout=2.0, poll_interval=0.25)
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=settings))
    scan = Mock(side_effect=[failure] * attempts + [None])
    monkeypatch.setattr('throngfission.execution.worker.janitor.reap', scan)
    monkeypatch.setattr('throngfission.execution.worker.janitor.monotonic', Mock(side_effect=[0.0, 0.1, 0.2]))
    pause = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.sleep', pause)
    main()
    assert scan.call_count == attempts + 1
    assert pause.call_count == attempts
    assert all(call.args == (0.25,) for call in pause.call_args_list)


@pytest.mark.parametrize('failure', [
    TimeoutError('API stalled'), HTTPError('url', 503, 'restarting', Message(), None),
])
def test_janitor_abandons_a_prolonged_api_outage(monkeypatch, failure):
    """The configured retry window bounds a failed cleanup job."""
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret', api_recovery_timeout=1.0)))
    scan = Mock(side_effect=failure)
    monkeypatch.setattr('throngfission.execution.worker.janitor.reap', scan)
    monkeypatch.setattr('throngfission.execution.worker.janitor.monotonic', Mock(side_effect=[0.0, 2.0]))
    pause = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.sleep', pause)
    with pytest.raises(type(failure)):
        main()
    scan.assert_called_once()
    pause.assert_not_called()


def test_janitor_does_not_retry_api_denial(monkeypatch):
    """RBAC errors remain visible rather than looking like control-plane outages."""
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret')))
    scan = Mock(side_effect=HTTPError('url', 403, 'forbidden', {}, None))
    monkeypatch.setattr('throngfission.execution.worker.janitor.reap', scan)
    pause = Mock()
    monkeypatch.setattr('throngfission.execution.worker.janitor.sleep', pause)
    with pytest.raises(HTTPError):
        main()
    scan.assert_called_once()
    pause.assert_not_called()
