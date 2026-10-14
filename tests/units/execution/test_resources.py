import base64
import subprocess
import sys

import pytest

from throngfission.execution.package import worker_archive
from throngfission.execution.resources import resources, root
from throngfission.execution.settings import Settings


@pytest.mark.parametrize('component', ['runtime', 'janitor'])
def test_manifest_entrypoints_load_from_the_worker_archive(component, tmp_path):
    """Both cluster entrypoints resolve inside the ZIP without the installed client or CLI."""
    archive = tmp_path / 'worker.zip'
    archive.write_bytes(base64.b64decode(worker_archive()))
    objects = {body['kind']: body for _, body in resources(Settings(), 'secret')}
    if component == 'runtime':
        bootstrap = base64.b64decode(objects['Package']['spec']['deployment']['literal']).decode()
    else:
        bootstrap = objects['CronJob']['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['command'][2]
    program = '''
import importlib
import sys
sys.path.insert(0, sys.argv[1])
module = importlib.import_module(f'throngfission.execution.worker.{sys.argv[2]}')
calls = []
module.main = lambda: calls.append(True)
namespace = {}
exec(sys.argv[3], namespace)
assert namespace['main'] is module.main
assert calls == ([True] if sys.argv[2] == 'janitor' else [])
assert 'throngfission.cli' not in sys.modules
assert 'throngfission.execution.manager' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-S', '-c', program, str(archive), component, bootstrap], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_resource_ownership_and_cleanup_policy():
    """One shared runner has a separate janitor identity and bounded job history."""
    settings = Settings(name='example', namespace='functions')
    objects = {body['kind']: body for _, body in resources(settings, 'secret')}
    assert len(objects) == 10
    assert base64.b64decode(objects['Secret']['data']['token']) == b'secret'
    function = objects['Function']['spec']
    assert function['onceOnly'] is True
    assert function['streaming'].get('maxDurationSeconds', 0) == 0
    assert function.get('retainPods', 0) == 0
    assert function['idletimeout'] == 120
    environment = objects['Environment']['spec']
    assert environment['poolsize'] == 2
    assert environment['resources'] == {'requests': {'cpu': '250m', 'memory': '256Mi'}}
    assert objects['CronJob']['spec']['concurrencyPolicy'] == 'Forbid'
    job = objects['CronJob']['spec']['jobTemplate']['spec']
    assert job['ttlSecondsAfterFinished'] == 60
    assert 'activeDeadlineSeconds' not in job
    assert job['template']['spec']['serviceAccountName'] == 'example'
    assert root(settings, 'digest', 'code')['binaryData'] == {'worker.zip': 'code'}


def test_resource_paths_and_http_route_preserve_namespace_and_name():
    """Every generated API collection and the public route retain their complete paths."""
    objects = resources(Settings(name='runner-02', namespace='compute-team'), 'secret')
    assert {body['kind']: path for path, body in objects} == {
        'Secret': '/api/v1/namespaces/compute-team/secrets',
        'ServiceAccount': '/api/v1/namespaces/compute-team/serviceaccounts',
        'Role': '/apis/rbac.authorization.k8s.io/v1/namespaces/compute-team/roles',
        'RoleBinding': '/apis/rbac.authorization.k8s.io/v1/namespaces/compute-team/rolebindings',
        'Environment': '/apis/fission.io/v1/namespaces/compute-team/environments',
        'Package': '/apis/fission.io/v1/namespaces/compute-team/packages',
        'Function': '/apis/fission.io/v1/namespaces/compute-team/functions',
        'HTTPTrigger': '/apis/fission.io/v1/namespaces/compute-team/httptriggers',
        'NetworkPolicy': '/apis/networking.k8s.io/v1/namespaces/compute-team/networkpolicies',
        'CronJob': '/apis/batch/v1/namespaces/compute-team/cronjobs',
    }
    trigger = next(body for _, body in objects if body['kind'] == 'HTTPTrigger')
    assert trigger['spec']['relativeurl'] == '/throngfission/compute-team/runner-02'


@pytest.mark.parametrize('image', ['python', 'debian', 'ubuntu', 'registry.example.com/team/fission-python:debian', f"registry.example.com:5000/team/runtime@sha256:{'a' * 64}"])
def test_selected_image_reaches_workers_and_cleanup_jobs(image):
    """Workers and janitors share a resolved image, identical for preset and full reference."""
    objects = {body['kind']: body for _, body in resources(Settings(image=image), 'secret')}
    reference = Settings(image=image).resolved_image
    assert objects['Environment']['spec']['runtime']['image'] == reference
    assert objects['CronJob']['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'] == reference
    assert resources(Settings(image=image), 'secret') == resources(Settings(image=reference), 'secret')


@pytest.mark.parametrize(('mode', 'rule'), [
    ('none', None),
    ('soft', 'preferredDuringSchedulingIgnoredDuringExecution'),
    ('hard', 'requiredDuringSchedulingIgnoredDuringExecution'),
])
def test_worker_anti_affinity_modes(mode, rule):
    """The scheduler spreads only workers of this function, with the selected strength."""
    settings = Settings(name='example', anti_affinity=mode)
    objects = {body['kind']: body for _, body in resources(settings, 'secret')}
    pod = objects['Environment']['spec']['runtime']['podspec']
    if rule is None:
        assert 'affinity' not in pod
    else:
        term = {'topologyKey': 'kubernetes.io/hostname', 'labelSelector': {'matchLabels': {'environmentName': 'example'}}}
        expected = [{'weight': 100, 'podAffinityTerm': term}] if mode == 'soft' else [term]
        assert pod['affinity'] == {'podAntiAffinity': {rule: expected}}
    assert 'affinity' not in objects['CronJob']['spec']['jobTemplate']['spec']['template']['spec']


def test_worker_root_is_confined_to_a_pod_user_namespace():
    """Worker root requires UID remapping without privileged mode or host namespaces."""
    objects = {body['kind']: body for _, body in resources(Settings(), 'secret')}
    runtime = objects['Environment']['spec']['runtime']
    pod = runtime['podspec']
    assert pod['hostUsers'] is False
    assert not any(pod.get(field, False) for field in ('hostNetwork', 'hostPID', 'hostIPC'))
    assert not any('hostPath' in volume for volume in pod['volumes'])
    assert runtime['container']['securityContext'] == {
        'runAsUser': 0, 'runAsGroup': 0, 'runAsNonRoot': False,
        'privileged': False, 'allowPrivilegeEscalation': False,
        'seccompProfile': {'type': 'RuntimeDefault'},
    }
    assert 'securityContext' not in pod['containers'][1]
    janitor = objects['CronJob']['spec']['jobTemplate']['spec']['template']['spec']
    assert janitor['hostUsers'] is False
    assert janitor['containers'][0]['securityContext'] == {
        'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True,
        'privileged': False, 'allowPrivilegeEscalation': False,
        'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'},
    }


def test_janitor_access_is_scoped_to_its_installation():
    """Health probes cross Fission's ingress policy without exposing other workers."""
    objects = {body['kind']: body for _, body in resources(Settings(name='example'), 'secret')}
    policy = objects['NetworkPolicy']['spec']
    assert policy['podSelector'] == {'matchLabels': {'throngfission.io/installation': 'example'}, 'matchExpressions': [{'key': 'executorType', 'operator': 'In', 'values': ['poolmgr']}]}
    assert policy['policyTypes'] == ['Ingress']
    executor, router, janitor = policy['ingress']
    assert executor == {'from': [{'namespaceSelector': {}, 'podSelector': {'matchLabels': {'svc': 'executor'}}}], 'ports': [{'protocol': 'TCP', 'port': 8000}, {'protocol': 'TCP', 'port': 8888}]}
    assert router == {'from': [{'namespaceSelector': {}, 'podSelector': {'matchLabels': {'application': 'fission-router', 'svc': 'router'}}}], 'ports': [{'protocol': 'TCP', 'port': 8888}]}
    labels = objects['CronJob']['spec']['jobTemplate']['spec']['template']['metadata']['labels']
    assert labels == {'throngfission.io/installation': 'example', 'throngfission.io/role': 'janitor'}
    assert janitor == {'from': [{'podSelector': {'matchLabels': labels}}], 'ports': [{'protocol': 'TCP', 'port': 8888}]}


@pytest.mark.parametrize('changes', [{}, {
    'function_idle_timeout': 160, 'stream_idle_timeout': 80, 'stream_max_duration': 300,
    'socket_tracker_interval': 3, 'termination_grace_period': 5, 'session_timeout': 90.0,
    'janitor_schedule': '*/2 * * * *', 'janitor_request_timeout': 7.5,
    'janitor_unreachable_timeout': 145.5, 'janitor_success_history': 2,
    'janitor_failure_history': 3, 'janitor_job_ttl': 90, 'janitor_backoff_limit': 2,
    'janitor_cpu_request': '25m', 'janitor_memory_request': '48Mi',
    'transfer_chunk_size': 524288, 'websocket_max_message_size': 2097152,
    'api_recovery_timeout': 45.0, 'poll_interval': 0.25,
}])
def test_configured_thresholds_reach_fission_and_janitor(changes):
    """Generated resources preserve both default and overridden lifecycle settings."""
    settings = Settings(**changes)
    objects = {body['kind']: body for _, body in resources(settings, 'secret')}
    function = objects['Function']['spec']
    assert function['idletimeout'] == settings.function_idle_timeout
    assert function['streaming']['protocol'] == 'websocket'
    assert function['streaming']['idleTimeoutSeconds'] == settings.stream_idle_timeout
    assert function['streaming'].get('maxDurationSeconds', 0) == settings.stream_max_duration
    environment = objects['Environment']['spec']
    assert environment['terminationGracePeriod'] == settings.termination_grace_period
    assert environment['runtime']['podspec']['terminationGracePeriodSeconds'] == settings.termination_grace_period
    assert environment['runtime']['podspec']['containers'][1] == {'name': 'fetcher', 'lifecycle': {'preStop': {'sleep': {'seconds': settings.termination_grace_period}}}}
    runtime_env = {variable['name']: variable.get('value') for variable in environment['runtime']['container']['env']}
    assert runtime_env['TIMEOUT'] == str(settings.socket_tracker_interval)
    assert runtime_env['THRONGFISSION_SESSION_TIMEOUT'] == str(settings.session_timeout)
    assert runtime_env['THRONGFISSION_TRANSFER_CHUNK_SIZE'] == str(settings.transfer_chunk_size)
    assert runtime_env['THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE'] == str(settings.websocket_max_message_size)
    cronjob = objects['CronJob']['spec']
    assert cronjob['schedule'] == settings.janitor_schedule
    assert cronjob['successfulJobsHistoryLimit'] == settings.janitor_success_history
    assert cronjob['failedJobsHistoryLimit'] == settings.janitor_failure_history
    job = cronjob['jobTemplate']['spec']
    assert job['ttlSecondsAfterFinished'] == settings.janitor_job_ttl
    assert job['backoffLimit'] == settings.janitor_backoff_limit
    container = job['template']['spec']['containers'][0]
    assert job['template']['spec']['terminationGracePeriodSeconds'] == settings.termination_grace_period
    assert container['resources'] == {'requests': {'cpu': settings.janitor_cpu_request, 'memory': settings.janitor_memory_request}}
    janitor_env = {variable['name']: variable.get('value') for variable in container['env']}
    assert janitor_env['THRONGFISSION_SESSION_TIMEOUT'] == str(settings.session_timeout)
    assert janitor_env['THRONGFISSION_API_RECOVERY_TIMEOUT'] == str(settings.api_recovery_timeout)
    assert janitor_env['THRONGFISSION_POLL_INTERVAL'] == str(settings.poll_interval)
    assert janitor_env['THRONGFISSION_JANITOR_REQUEST_TIMEOUT'] == str(settings.janitor_request_timeout)
    assert janitor_env['THRONGFISSION_JANITOR_UNREACHABLE_TIMEOUT'] == str(settings.janitor_unreachable_timeout)
