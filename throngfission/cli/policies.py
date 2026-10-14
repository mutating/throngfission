import json
from typing import Dict


def user_namespace_policy(apparmor_profile: str = '') -> Dict[str, object]:
    """Keep pod UID remapping when Fission omits hostUsers from its Deployment."""
    name = 'throngfission-user-namespaces'
    version = 'admissionregistration.k8s.io/v1'
    specification = 'hostUsers: false'
    if apparmor_profile:
        specification += (
            ', securityContext: Object.spec.securityContext{'
            'appArmorProfile: Object.spec.securityContext.appArmorProfile{'
            f'type: "Localhost", localhostProfile: {json.dumps(apparmor_profile)}'
            '}}'
        )
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': version, 'kind': 'MutatingAdmissionPolicy', 'metadata': {'name': name}, 'spec': {
            'matchConstraints': {
                'objectSelector': {'matchExpressions': [{'key': 'throngfission.io/installation', 'operator': 'Exists'}]},
                'resourceRules': [{'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['CREATE'], 'resources': ['pods']}],
            },
            'failurePolicy': 'Fail', 'reinvocationPolicy': 'IfNeeded',
            'mutations': [{'patchType': 'ApplyConfiguration', 'applyConfiguration': {'expression': f'Object{{spec: Object.spec{{{specification}}}}}'}}],
        }},
        {'apiVersion': version, 'kind': 'MutatingAdmissionPolicyBinding', 'metadata': {'name': name}, 'spec': {'policyName': name}},
    ]}
