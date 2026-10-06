from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from cantok import CancellationError, SimpleToken
from locklib import LockTraceWrapper, SmartLock
from websocket import WebSocketBadStatusException, WebSocketTimeoutException

from throngfission.cluster import Cluster
from throngfission.errors import APIError, CleanupError, FissionError
from throngfission.settings import Settings


def test_bootstrap_is_shared_between_threads(monkeypatch):
    """Concurrent callers install once and reuse the secret under a traced lock."""
    api = Mock(request=Mock(side_effect=lambda *_args, **_kwargs: (lock.notify('admission'), {'spec': {'hostUsers': False}})[1]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cluster.worker_archive', Mock(return_value='archive'))
    cluster = Cluster(Settings())
    lock = LockTraceWrapper(SmartLock())
    cluster.lock = lock
    create = Mock(side_effect=lambda _, body: (lock.notify('create'), {**body, 'metadata': {**body['metadata'], 'uid': 'owner'}})[1])
    monkeypatch.setattr(cluster, 'create', create)
    with ThreadPoolExecutor(max_workers=3) as executor:
        values = list(executor.map(lambda _: cluster.ensure(), range(3)))
    assert len(set(values)) == 1
    assert create.call_count == 11
    assert lock.was_event_locked('create')
    assert lock.was_event_locked('admission')
    api.request.assert_called_once()
    assert create.call_args.args[1]['metadata']['ownerReferences'][0]['uid'] == 'owner'
    assert create.call_args.args[1]['metadata']['ownerReferences'][0]['blockOwnerDeletion']


def test_conflicting_installation_is_not_updated(monkeypatch):
    """An incompatible installation cannot disrupt existing sessions."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(return_value={'spec': {'hostUsers': False}}))))
    monkeypatch.setattr('throngfission.cluster.worker_archive', Mock(return_value='archive'))
    cluster = Cluster(Settings())
    monkeypatch.setattr(cluster, 'create', Mock(return_value={'data': {'fingerprint': 'different'}}))
    with pytest.raises(FissionError, match='different code'):
        cluster.ensure()


@pytest.mark.parametrize(('saved_mode', 'current_mode', 'active'), [
    ('none', 'soft', False), ('soft', 'hard', False), ('hard', 'none', False),
    ('none', 'soft', True), ('other-settings', 'soft', False), ('soft', 'soft', False),
])
def test_explicit_reconfiguration_changes_only_idle_scheduling(saved_mode, current_mode, active, monkeypatch):
    """Setup replaces an idle function only for a known mode change, never for unrelated drift."""
    settings = Settings(anti_affinity=current_mode)
    saved = Cluster.fingerprint(Settings(anti_affinity=saved_mode), 'archive') if saved_mode != 'other-settings' else 'unrelated'
    api = Mock(request=Mock(side_effect=[{'data': {'fingerprint': saved}}, {'items': [{'metadata': {'name': 'busy'}}] if active else []}]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cluster.worker_archive', Mock(return_value='archive'))
    cluster = Cluster(settings)
    uninstall = Mock()
    monkeypatch.setattr(cluster, 'uninstall', uninstall)
    if active:
        with pytest.raises(FissionError, match='active isolates'):
            cluster.reconfigure()
    else:
        cluster.reconfigure()
    assert uninstall.call_count == (1 if saved_mode not in ('other-settings', current_mode) and not active else 0)
    assert api.request.call_count == (2 if saved_mode not in ('other-settings', current_mode) else 1)
    if api.request.call_count == 2:
        assert api.request.call_args.kwargs == {'query': [('labelSelector', 'throngfission.io/installation=' + settings.function_name + ',managed=false')]}


@pytest.mark.parametrize('status', [404, 403])
def test_reconfiguration_requires_a_readable_owned_installation(status, monkeypatch):
    """A missing function is left for normal setup, while API failures surface."""
    api = Mock(request=Mock(side_effect=APIError(status, 'unavailable')))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    if status == 404:
        cluster.reconfigure()
    else:
        with pytest.raises(APIError, match='unavailable'):
            cluster.reconfigure()
    api.request.assert_called_once_with('GET', cluster.core + 'configmaps/' + cluster.settings.function_name)


@pytest.mark.parametrize('specification', [{}, {'hostUsers': True}, {'hostUsers': None}, {'hostUsers': 0}])
def test_missing_admission_prevents_creating_root_workers(monkeypatch, specification):
    """A non-mutating dry-run creates neither shared resources nor unsafe warm workers."""
    api = Mock(request=Mock(return_value={'spec': specification}))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    create = Mock()
    monkeypatch.setattr(cluster, 'create', create)
    with pytest.raises(FissionError, match='admission policy'):
        cluster.ensure()
    create.assert_not_called()
    assert cluster.secret is None
    api.request.assert_called_once()
    method, path, body = api.request.call_args.args
    assert (method, path) == ('POST', cluster.core + 'pods')
    assert api.request.call_args.kwargs == {'query': [('dryRun', 'All')]}
    assert body['metadata']['labels'] == {'throngfission.io/installation': cluster.settings.function_name}
    assert body['spec']['hostUsers'] is True
    assert body['spec']['containers'][0]['image'] == cluster.settings.resolved_image


@pytest.mark.parametrize('status', [403, 409])
def test_create_preserves_foreign_resources(monkeypatch, status):
    """Permission failures propagate and name conflicts never adopt foreign objects."""
    responses = [APIError(403, 'failed')] if status == 403 else [APIError(404, 'missing'), APIError(409, 'exists'), {'metadata': {}}]
    api = Mock(request=Mock(side_effect=responses))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    with pytest.raises(FissionError):
        cluster.create('/pods', {})
    assert api.request.call_count == len(responses)


def test_create_recovers_owned_conflict(monkeypatch):
    """A competing bootstrap may reuse objects already created by this installation."""
    body = {'metadata': {'labels': {'throngfission.io/installation': 'throngfission-python'}}}
    api = Mock(request=Mock(side_effect=[APIError(404, 'missing'), APIError(409, 'exists'), body, body]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    assert cluster.create('/pods', body) == body
    assert cluster.create('/pods', body) == body


@pytest.mark.parametrize('outcome', [{'metadata': {'uid': 'new'}}, APIError(403, 'denied')])
def test_creation_after_missing_read_preserves_result_and_errors(monkeypatch, outcome):
    """Only a missing object is created, and non-conflict creation errors propagate."""
    api = Mock(request=Mock(side_effect=[APIError(404, 'missing'), outcome]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    cluster = Cluster(Settings())
    if isinstance(outcome, APIError):
        with pytest.raises(APIError, match='denied'):
            cluster.create('/pods', {'kind': 'Pod'})
    else:
        assert cluster.create('/pods', {'kind': 'Pod'}) == outcome
    assert [call.args[0] for call in api.request.call_args_list] == ['GET', 'POST']


@pytest.mark.parametrize('router', ['', 'https://router.example'])
def test_connect_uses_configured_route(monkeypatch, router):
    """Direct routers never receive Kubernetes credentials; proxy routes retain them."""
    api = Mock(connection=Mock(return_value=('wss://cluster', {'Authorization': 'Bearer kube'}, {'certfile': 'cert'})))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    socket = Mock(recv=Mock(return_value='{"protocol":2,"pod":"worker","uid":"uid"}'))
    connect = Mock(return_value=socket)
    monkeypatch.setattr('websocket.create_connection', connect)
    cluster = Cluster(Settings(router_url=router))
    cluster.secret = 'secret'
    monkeypatch.setattr(cluster, 'owned_pod', Mock(return_value={'spec': {'hostUsers': False}}))
    connection, pod, uid = cluster.connect(SimpleToken())
    assert connection.socket is socket
    assert (pod, uid) == ('worker', 'uid')
    assert ('Authorization' in connect.call_args.kwargs['header']) is (not router)
    assert connect.call_args.kwargs['header']['X-Throngfission-Token'] == 'secret'
    assert connect.call_args.args[0].endswith('/throngfission/fission-function/throngfission-python')


@pytest.mark.parametrize('failure', [WebSocketTimeoutException('timeout'), WebSocketBadStatusException('busy', status_code=503)])
def test_capacity_failure_waits_then_connects(monkeypatch, failure):
    """Temporary handshake failures wait for the pool instead of escaping get()."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(connection=Mock(return_value=('wss://cluster', {}, {})))))
    socket = Mock(recv=Mock(return_value='{"protocol":2,"pod":"worker","uid":"uid"}'))
    monkeypatch.setattr('websocket.create_connection', Mock(side_effect=[failure, socket]))
    cluster = Cluster(Settings())
    cluster.secret = 'secret'
    wait = Mock()
    monkeypatch.setattr(cluster, 'wait_for_pool', wait)
    monkeypatch.setattr(cluster, 'owned_pod', Mock(return_value={'spec': {'hostUsers': False}}))
    token = SimpleToken()
    assert cluster.connect(token)[1] == 'worker'
    wait.assert_called_once_with(token)


def test_handshake_permission_failure_is_not_capacity(monkeypatch):
    """Authentication errors are reported rather than retried indefinitely."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(connection=Mock(return_value=('wss://cluster', {}, {})))))
    monkeypatch.setattr('websocket.create_connection', Mock(side_effect=WebSocketBadStatusException('denied', status_code=403)))
    cluster = Cluster(Settings())
    cluster.secret = 'secret'
    with pytest.raises(FissionError, match='rejected'):
        cluster.connect(SimpleToken())


def test_invalid_greeting_closes_socket(monkeypatch):
    """A protocol mismatch closes the uninitialized session immediately."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(connection=Mock(return_value=('wss://cluster', {}, {})))))
    socket = Mock(recv=Mock(return_value='{"protocol":1}'))
    monkeypatch.setattr('websocket.create_connection', Mock(return_value=socket))
    cluster = Cluster(Settings())
    cluster.secret = 'secret'
    with pytest.raises(FissionError, match='protocol'):
        cluster.connect(SimpleToken())
    socket.abort.assert_called_once()


@pytest.mark.parametrize('specification', [{}, {'hostUsers': True}, {'hostUsers': None}, {'hostUsers': 0}])
def test_unmapped_worker_is_deleted_before_commands_can_run(monkeypatch, specification):
    """Missing or invalid UID isolation closes and deletes the worker without sending commands."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(connection=Mock(return_value=('wss://cluster', {}, {})))))
    socket = Mock(recv=Mock(return_value='{"protocol":2,"pod":"worker","uid":"uid"}'))
    monkeypatch.setattr('websocket.create_connection', Mock(return_value=socket))
    cluster = Cluster(Settings())
    cluster.secret = 'secret'
    monkeypatch.setattr(cluster, 'owned_pod', Mock(return_value={'spec': specification}))
    delete = Mock()
    monkeypatch.setattr(cluster, 'delete_pod', delete)
    with pytest.raises(FissionError, match='user namespace'):
        cluster.connect(SimpleToken())
    delete.assert_called_once_with('worker', 'uid')
    socket.abort.assert_called_once()
    socket.send.assert_not_called()


def test_pending_pool_waits_for_readiness(monkeypatch):
    """Pending pods keep allocation waiting until Kubernetes reports Ready."""
    api = Mock(request=Mock(side_effect=[{'items': [{'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}}]}, {'items': [{'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'containerStatuses': [{'state': {'running': {}}}]}}]}]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    sleep = Mock()
    monkeypatch.setattr('throngfission.cluster.sleep', sleep)
    Cluster(Settings()).wait_for_pool(SimpleToken())
    assert api.request.call_count == 2
    assert sleep.call_count == 2


def test_capacity_wait_can_be_cancelled(monkeypatch):
    """Cancellation ends capacity waiting even when the cluster never becomes ready."""
    token = SimpleToken()
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(return_value={'items': []}))))
    monkeypatch.setattr('throngfission.cluster.sleep', lambda _: token.cancel())
    with pytest.raises(CancellationError):
        Cluster(Settings()).wait_for_pool(token)


def test_permanent_pod_configuration_failure(monkeypatch):
    """Invalid container configuration is not mistaken for occupied resources."""
    pod = {'status': {'containerStatuses': [{'state': {'waiting': {'reason': 'InvalidImageName', 'message': 'bad image'}}}]}}
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(return_value={'items': [pod]}))))
    with pytest.raises(FissionError, match='bad image'):
        Cluster(Settings()).wait_for_pool(SimpleToken())


@pytest.mark.parametrize('changes', [{'uid': 'other'}, {'labels': {}}, {'labels': {'throngfission.io/installation': 'throngfission-python', 'managed': 'true'}}])
def test_pod_identity_is_checked(monkeypatch, changes):
    """Cleanup cannot target a foreign, replacement or warm-pool pod."""
    pod = {'metadata': {'uid': 'uid', 'labels': {'throngfission.io/installation': 'throngfission-python', 'managed': 'false'}, **changes}}
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(return_value=pod))))
    with pytest.raises(FissionError, match='outside'):
        Cluster(Settings()).owned_pod('worker', 'uid')


@pytest.mark.parametrize('status', [404, 403])
def test_delete_handles_missing_and_forbidden_pods(monkeypatch, status):
    """A missing pod is already clean; insufficient permissions remain an error."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(side_effect=APIError(status, 'failed')))))
    cluster = Cluster(Settings())
    if status == 404:
        assert cluster.delete_pod('worker', 'uid') is None
    else:
        with pytest.raises(APIError):
            cluster.delete_pod('worker', 'uid')


@pytest.mark.parametrize('outcome', [APIError(404, 'gone'), {'metadata': {'uid': 'replacement'}}, APIError(403, 'denied')])
def test_delete_waits_for_the_original_uid(monkeypatch, outcome):
    """Deletion waits through Terminating and never confuses a replacement pod."""
    pod = {'metadata': {'uid': 'uid', 'labels': {'throngfission.io/installation': 'throngfission-python', 'managed': 'false'}}}
    api = Mock(request=Mock(side_effect=[pod, {}, pod, outcome]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cluster.sleep', Mock())
    if isinstance(outcome, APIError) and outcome.status == 403:
        with pytest.raises(APIError):
            Cluster(Settings()).delete_pod('worker', 'uid')
    else:
        Cluster(Settings()).delete_pod('worker', 'uid')
    assert api.request.call_args_list[1].args[2]['preconditions'] == {'uid': 'uid'}


def test_delete_reports_unconfirmed_cleanup(monkeypatch):
    """A pod that never disappears produces a cleanup failure rather than success."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock())
    cluster = Cluster(Settings())
    monkeypatch.setattr(cluster, 'owned_pod', Mock())
    monkeypatch.setattr('throngfission.cluster.monotonic', Mock(side_effect=[0, 121]))
    with pytest.raises(CleanupError):
        cluster.delete_pod('worker', 'uid')


def test_uninstall_removes_the_shared_owner(monkeypatch):
    """Uninstall holds the bootstrap lock through deletion and verifies its release."""
    owner = {'metadata': {'uid': 'uid', 'labels': {'throngfission.io/installation': 'throngfission-python'}}}
    api = Mock(request=Mock(side_effect=[owner, {}, APIError(404, 'gone'), {'items': [{'metadata': {'name': 'worker'}}]}, {'items': []}]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cluster.sleep', Mock())
    cluster = Cluster(Settings())
    cluster.secret = 'secret'
    cluster.lock = LockTraceWrapper(SmartLock())
    request = api.request
    api.request = Mock(side_effect=lambda *args, **kwargs: (cluster.lock.notify('request'), request(*args, **kwargs))[1])
    cluster.uninstall()
    cluster.lock.notify('finished')
    assert cluster.lock.was_event_locked('request')
    assert not cluster.lock.was_event_locked('finished')
    assert cluster.secret is None
    assert api.request.call_args_list[1].args == ('DELETE', '/api/v1/namespaces/fission-function/configmaps/throngfission-python', {'propagationPolicy': 'Foreground', 'preconditions': {'uid': 'uid'}})
    assert api.request.call_count == 5


@pytest.mark.parametrize('status', [404, 403])
def test_uninstall_handles_missing_and_forbidden_owners(monkeypatch, status):
    """Repeated uninstall is safe, while denied access cannot appear successful."""
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=Mock(request=Mock(side_effect=APIError(status, 'failed')))))
    cluster = Cluster(Settings())
    if status == 403:
        with pytest.raises(APIError):
            cluster.uninstall()
    else:
        cluster.uninstall()
        assert cluster.secret is None


def test_uninstall_refuses_an_unrelated_owner(monkeypatch):
    """A matching ConfigMap name alone never authorizes cascading deletion."""
    api = Mock(request=Mock(return_value={'metadata': {}}))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    with pytest.raises(FissionError, match='Refusing to uninstall'):
        Cluster(Settings()).uninstall()
    api.request.assert_called_once()


def test_uninstall_does_not_hide_failed_pod_verification(monkeypatch):
    """A missing owner does not hide denied access when checking remaining pods."""
    api = Mock(request=Mock(side_effect=[APIError(404, 'gone'), APIError(403, 'denied')]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    with pytest.raises(APIError, match='denied'):
        Cluster(Settings()).uninstall()


def test_uninstall_times_out_on_orphaned_pods(monkeypatch):
    """An owner disappearing is insufficient evidence that its workers are gone."""
    api = Mock(request=Mock(side_effect=[APIError(404, 'gone'), {'items': [{}]}]))
    monkeypatch.setattr('throngfission.cluster.Kubernetes', Mock(return_value=api))
    monkeypatch.setattr('throngfission.cluster.monotonic', Mock(side_effect=[0, 121]))
    cluster = Cluster(Settings())
    cluster.secret = 'old'
    with pytest.raises(CleanupError, match='still being deleted'):
        cluster.uninstall()
    assert cluster.secret is None
