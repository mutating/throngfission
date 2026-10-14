import json
import subprocess
import sys
from textwrap import dedent
from time import sleep

import pytest
from cantok import TimeoutToken

from throngfission import Settings
from throngfission.execution.errors import APIError, IsolateClosedError


@pytest.mark.parametrize('abandoned', [False, True])
def test_janitor_keeps_live_sessions_and_removes_abandoned_pods(isolate, manager, abandoned):
    """The real cleanup Job can reach live workers and deletes only expired sessions."""
    if abandoned:
        isolate.stopped.set()
        isolate.thread.join(timeout=5)
        assert not isolate.thread.is_alive()
        isolate.connection.socket.settimeout(45)
        with pytest.raises(IsolateClosedError):
            isolate.connection.receive()
    batch = f'/apis/batch/v1/namespaces/{manager.settings.namespace}/'
    cronjob = manager.cluster.api.request('GET', f'{batch}cronjobs/{manager.settings.name}')
    job = manager.cluster.api.request('POST', f'{batch}jobs', {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': f'{manager.settings.name}-probe', 'labels': {'throngfission.io/installation': manager.settings.name}, 'ownerReferences': [{'apiVersion': 'batch/v1', 'kind': 'CronJob', 'name': manager.settings.name, 'uid': cronjob['metadata']['uid']}]},
        'spec': cronjob['spec']['jobTemplate']['spec'],
    })
    token = TimeoutToken(120)
    while True:
        token.check()
        job = manager.cluster.api.request('GET', f"{batch}jobs/{job['metadata']['name']}")
        assert not job.get('status', {}).get('failed'), job
        if job.get('status', {}).get('succeeded'):
            break
        sleep(0.2)
    if not abandoned:
        pod = manager.cluster.api.request('GET', f'{manager.cluster.core}pods/{isolate.pod}')
        assert 'throngfission.io/unreachable-since' not in pod['metadata'].get('annotations', {})
        assert isolate.run('true').success
        return
    while True:
        token.check()
        try:
            manager.cluster.api.request('GET', f'{manager.cluster.core}pods/{isolate.pod}')
        except APIError as error:
            if error.status != 404:
                raise
            break
        sleep(0.2)


@pytest.mark.parametrize('phase', ['ready', 'upload', 'download'])
def test_scheduled_janitor_cleans_up_after_client_process_crash(manager, phase):
    """Client crashes during commands or snapshot transfers leave no unreaped worker."""
    manager.settings.function_idle_timeout = 600
    manager.settings.janitor_success_history = 1
    manager.settings.janitor_job_ttl = 300
    settings = {name: getattr(manager.settings, name) for name in Settings.__annotations__}
    result = subprocess.run([sys.executable, '-c', dedent('''
        import json
        import base64
        import os
        import sys
        from cantok import TimeoutToken
        from throngfission import FissionManager, Settings

        manager = FissionManager(sys.argv[1], settings=Settings(**json.loads(sys.argv[2])))
        if sys.argv[3] == 'upload':
            connection, pod, uid = manager.cluster.connect(TimeoutToken(180))
            connection.request({'op': 'init', 'size': 10485760})
            connection.request({'op': 'upload', 'state': base64.b64encode(b'partial').decode()})
        else:
            isolate = manager.get(manager.read(), TimeoutToken(180))
            assert isolate.run('true').success
            pod, uid = isolate.pod, isolate.uid
            if sys.argv[3] == 'download':
                isolate.connection.request({'op': 'read'})
                assert isolate.connection.request({'op': 'download'})['state']
        print(json.dumps({'pod': pod, 'uid': uid}), flush=True)
        os._exit(23)
    '''), str(manager.path), json.dumps(settings), phase], capture_output=True, text=True, timeout=210, check=False)
    assert result.returncode == 23, result.stderr
    worker = json.loads(result.stdout)
    token = TimeoutToken(180)
    while True:
        token.check()
        try:
            pod = manager.cluster.api.request('GET', f"{manager.cluster.core}pods/{worker['pod']}")
            assert pod['metadata']['uid'] == worker['uid']
        except APIError as error:
            if error.status != 404:
                raise
            break
        sleep(0.2)
    jobs = manager.cluster.api.request('GET', f'/apis/batch/v1/namespaces/{manager.settings.namespace}/jobs')['items']
    assert any(job['metadata'].get('ownerReferences', [{}])[0].get('name') == manager.settings.name for job in jobs)
