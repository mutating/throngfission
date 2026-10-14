"""Prepare only missing runtime images on individual Ready K3s nodes."""

import sys
from pathlib import Path
from subprocess import run
from time import sleep
from typing import Dict, List, cast

from cantok import TimeoutToken

from throngfission.cli.settings import SetupSettings
from throngfission.execution.api import Kubernetes
from throngfission.execution.errors import FissionError
from throngfission.execution.settings import Settings


class Provision:
    def __init__(self, api: Kubernetes, settings: Settings, options: SetupSettings) -> None:
        self.api, self.settings, self.options = api, settings, options

    def nodes(self) -> List[Dict[str, object]]:
        ready = []
        for node in cast(List[Dict[str, object]], self.api.request('GET', '/api/v1/nodes')['items']):
            status = cast(Dict[str, object], node['status'])
            name = str(cast(Dict[str, object], node['metadata'])['name'])
            if not any(item['type'] == 'Ready' and item['status'] == 'True' for item in cast(List[Dict[str, str]], status['conditions'])):
                sys.stdout.write(f'Offline node skipped; rerun setup when it returns: {name}\n')
                continue
            info = cast(Dict[str, str], status['nodeInfo'])
            if '+k3s' not in info['kubeletVersion'] or info['architecture'] not in ('arm64', 'amd64') or info['operatingSystem'] != 'linux':
                raise FissionError(f'Runtime provisioning requires Linux K3s on arm64 or amd64: {name}')
            ready.append(node)
        if not ready:
            raise FissionError('No Ready K3s nodes are available for setup.')
        return ready

    def command(self, script: str) -> List[str]:
        arguments = ['bash', str(Path(__file__).with_name('assets') / script), '--namespace', self.settings.namespace, '--timeout', str(self.options.image_timeout), '--cleanup-ttl', str(self.options.image_cleanup_ttl), '--work-dir', self.options.work_dir]
        for flag, value in (('kubeconfig', self.settings.kubeconfig), ('context', self.settings.context)):
            if value:
                arguments.extend([f'--{flag}', value])
        return arguments

    def images(self, nodes: List[Dict[str, object]]) -> None:
        for preset in ('python', 'debian', 'ubuntu'):
            reference = Settings(image=preset).resolved_image
            missing: Dict[str, List[str]] = {}
            for node in nodes:
                status = cast(Dict[str, object], node['status'])
                available = [name for image in cast(List[Dict[str, object]], status.get('images') or []) for name in cast(List[str], image.get('names') or [])]
                if reference not in available:
                    architecture = cast(Dict[str, str], status['nodeInfo'])['architecture']
                    missing.setdefault(architecture, []).append(str(cast(Dict[str, object], node['metadata'])['name']))
            for architecture, names in missing.items():
                sys.stdout.write(f"Preparing {preset} on: {', '.join(names)}\n")
                run([*self.command('build_image.sh'), '--image', preset, '--platform', f'linux/{architecture}', '--nodes', ','.join(names)], check=True)
                for name in names:
                    self.wait_image(name, reference)

    def wait_image(self, name: str, reference: str) -> None:
        """Wait for kubelet's inventory so an immediate rerun does not reimport images."""
        deadline = TimeoutToken(self.options.image_timeout)
        while True:
            deadline.check()
            node = self.api.request('GET', f'/api/v1/nodes/{name}')
            status = cast(Dict[str, object], node['status'])
            if any(reference in cast(List[str], image.get('names') or []) for image in cast(List[Dict[str, object]], status.get('images') or [])):
                return
            sleep(self.options.setup_poll_interval)

    def apparmor(self, nodes: List[Dict[str, object]]) -> str:
        profile = self.options.apparmor_profile
        if profile == 'auto':
            affected = any(cast(Dict[str, str], cast(Dict[str, object], node['status'])['nodeInfo'])['osImage'].startswith('Ubuntu 26.04') for node in nodes)
            profile = 'throngfission-stacked-v1' if affected else ''
        if profile != 'throngfission-stacked-v1':
            return profile
        missing = [node for node in nodes if cast(Dict[str, str], cast(Dict[str, object], node['metadata']).get('annotations', {})).get('throngfission.io/apparmor-profile') != profile]
        if missing:
            names = [str(cast(Dict[str, object], node['metadata'])['name']) for node in missing]
            run([*self.command('install_apparmor.sh'), '--nodes', ','.join(names)], check=True)
            for name in names:
                self.api.request('PATCH', f'/api/v1/nodes/{name}', {'metadata': {'annotations': {'throngfission.io/apparmor-profile': profile}}})
        return profile
