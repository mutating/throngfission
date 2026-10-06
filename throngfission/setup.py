"""Resumable setup of Fission and the three shared isolate functions."""

import base64
import hashlib
import json
import secrets
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from typing import Dict, List, Optional, cast

from cantok import TimeoutToken

from throngfission.api import Kubernetes
from throngfission.certificates import certificate
from throngfission.errors import FissionError, IsolateClosedError
from throngfission.manager import FissionManager
from throngfission.manifests import Manifests, contains
from throngfission.provision import Provision
from throngfission.resources import resources, user_namespace_policy
from throngfission.settings import Settings, SetupSettings


class Setup:
    def __init__(self, settings: Settings, options: Optional[SetupSettings] = None) -> None:
        self.settings = settings
        self.options = options if options is not None else SetupSettings.from_env()  # type: ignore[misc]
        settings.validate()
        self.options.validate()
        if settings.name:
            raise ValueError('Setup uses python_name, debian_name and ubuntu_name; unset THRONGFISSION_NAME.')
        if len({settings.python_name, settings.debian_name, settings.ubuntu_name}) != 3:
            raise ValueError('The three preset function names must be different.')
        self.api = Kubernetes(settings)
        self.manifests = Manifests(self.api, self.options)

    def run(self) -> None:
        try:
            version = self.api.request('GET', '/version')
            if (int(str(version['major'])), int(str(version['minor']).rstrip('+'))) < (1, 36):
                raise FissionError('Setup requires Kubernetes 1.36+ for pod user namespaces and admission policies.')
            provision = Provision(self.api, self.settings, self.options)
            nodes = provision.nodes()
            sys.stdout.write('Cluster reachable; checking Fission.\n')
            self.fission()
            provision.images(nodes)
            profile = provision.apparmor(nodes)
            for body in cast(List[Dict[str, object]], user_namespace_policy(profile)['items']):
                existing, _ = self.manifests.ensure(body)
                if not contains(existing.get('spec'), body['spec']):
                    raise FissionError('The existing throngfission admission policy has different settings.')
            self.admission()
            for preset in ('python', 'debian', 'ubuntu'):
                self.function(preset)
            sys.stdout.write('Setup complete: python, debian, ubuntu.\n')
        finally:
            self.api.close()

    def fission(self) -> None:
        control = self.settings.router_namespace
        marker_path = '/api/v1/namespaces/' + control + '/configmaps/throngfission-setup'
        marker = self.manifests.get(marker_path)
        executor_path = '/apis/apps/v1/namespaces/' + control + '/deployments/executor'
        if marker is None and self.manifests.get(executor_path) is not None:
            sys.stdout.write('Existing Fission installation retained.\n')
            return
        for namespace in {control, self.settings.namespace}:
            self.manifests.ensure({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace}})
        self.manifests.ensure({'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'throngfission-setup', 'namespace': control}, 'data': {'version': '1.27.0'}})
        text = Path(__file__).with_name('fission-v1.27.0.json').read_text()
        text = text.replace('__CONTROL_NAMESPACE__', json.dumps(control)[1:-1]).replace('__FUNCTION_NAMESPACE__', json.dumps(self.settings.namespace)[1:-1])
        bodies = cast(List[Dict[str, object]], json.loads(text))
        credentials = self.credentials()
        for body in bodies:
            metadata = cast(Dict[str, object], body['metadata'])
            if body['kind'] == 'Secret':
                body['data'] = credentials[str(metadata['name'])]
            if str(body['kind']).endswith('WebhookConfiguration'):
                for hook in cast(List[Dict[str, object]], body['webhooks']):
                    cast(Dict[str, object], hook['clientConfig'])['caBundle'] = credentials['fission-webhook-certs']['ca.crt']
            self.manifests.ensure(body)
            if body['kind'] == 'CustomResourceDefinition':
                self.manifests.wait(body, 'Established')
        for body in bodies:
            if body['kind'] == 'Deployment':
                self.manifests.wait(body, 'Available')

    def credentials(self) -> Dict[str, Dict[str, str]]:
        """Reuse cluster-local credentials across interrupted and repeated installations."""
        prefix = '/api/v1/namespaces/' + self.settings.router_namespace + '/secrets/'
        auth = self.manifests.get(prefix + 'fission-internal-auth')
        if auth is None:
            auth = self.manifests.get('/api/v1/namespaces/' + self.settings.namespace + '/secrets/fission-internal-auth')
        auth_data = cast(Dict[str, str], auth['data']) if auth is not None else {'secret': base64.b64encode(secrets.token_urlsafe(32).encode()).decode()}
        auth, _ = self.manifests.ensure({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fission-internal-auth', 'namespace': self.settings.router_namespace}, 'data': auth_data})
        cert = self.manifests.get(prefix + 'fission-webhook-certs')
        cert_data = cast(Dict[str, str], cert['data']) if cert is not None else certificate(self.settings.router_namespace, self.options)
        cert, _ = self.manifests.ensure({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'fission-webhook-certs', 'namespace': self.settings.router_namespace}, 'data': cert_data})
        return {'fission-internal-auth': cast(Dict[str, str], auth['data']), 'fission-webhook-certs': cast(Dict[str, str], cert['data'])}

    def admission(self) -> None:
        deadline = TimeoutToken(self.options.setup_timeout)
        while True:
            deadline.check()
            probe = self.api.request('POST', '/api/v1/namespaces/' + self.settings.namespace + '/pods', {
                'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'generateName': 'throngfission-check-', 'labels': {'throngfission.io/installation': 'setup'}},
                'spec': {'hostUsers': True, 'automountServiceAccountToken': False, 'containers': [{'name': 'check', 'image': Settings().resolved_image}]},
            }, query=[('dryRun', 'All')])
            if cast(Dict[str, object], probe['spec']).get('hostUsers') is False:
                return
            sleep(self.options.setup_poll_interval)

    def function(self, preset: str) -> None:
        settings = Settings(**{field: getattr(self.settings, field) for field in Settings.__annotations__})  # type: ignore[misc]
        settings.image = preset
        with TemporaryDirectory(prefix='throngfission-diagnostic-', dir=self.options.work_dir) as directory:
            manager = FissionManager(directory, settings=settings)
            cluster = manager.cluster
            try:
                cluster.reconfigure()
                cluster.ensure()
                path = cluster.core + 'configmaps/' + settings.function_name
                owner = self.api.request('GET', path)
                identities = [cast(Dict[str, str], self.api.request('GET', collection + '/' + settings.function_name)['metadata'])['uid'] for collection, _ in resources(settings, '')]
                digest = hashlib.sha256(json.dumps(identities).encode()).hexdigest()
                annotations = cast(Dict[str, str], cast(Dict[str, object], owner['metadata']).get('annotations', {}))
                if annotations.get('throngfission.io/diagnostic') == digest:
                    sys.stdout.write('Function already prepared: ' + settings.function_name + '\n')
                    return
                sys.stdout.write('Diagnosing ' + preset + ' as ' + settings.function_name + '\n')
                token = TimeoutToken(self.options.diagnostic_timeout)
                state = manager.read()
                while True:
                    token.check()
                    try:
                        isolate = manager.get(state, token)
                        break
                    except IsolateClosedError:
                        sys.stdout.write('Diagnostic connection lost before commands; waiting for a new worker.\n')
                        sleep(self.options.setup_poll_interval)
                try:
                    result = isolate.run('python -c "import os,pathlib,subprocess; assert os.geteuid()==0; assert int(pathlib.Path(\'/proc/self/uid_map\').read_text().split()[1])!=0; child=subprocess.Popen([\'python\',\'-c\',\'import time; time.sleep(60)\']); child.terminate(); child.wait(); print(\'throngfission-ready\')"', token)
                    if not result.success or result.stdout != 'throngfission-ready\n':
                        raise FissionError('Runtime diagnostic failed for ' + preset + ': ' + str(result.stderr) + str(result.stdout))
                    for check in isolate.chain('sh -c "exit 0"', 'python -m pip --version', token=token):
                        if not check.success:
                            raise FissionError('Shell/pip diagnostic failed for ' + preset + ': ' + str(check.stderr))
                finally:
                    isolate.kill()
                self.api.request('PATCH', path, {'metadata': {'annotations': {'throngfission.io/diagnostic': digest}}})
            finally:
                cluster.api.close()
