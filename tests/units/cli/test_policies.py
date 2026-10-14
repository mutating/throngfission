import pytest

from throngfission.cli.policies import user_namespace_policy


@pytest.mark.parametrize(('profile', 'expression'), [
    ('', 'Object{spec: Object.spec{hostUsers: false}}'),
    ('throngfission', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "throngfission"}}}}'),
    ('quoted"profile', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "quoted\\"profile"}}}}'),
    ('back\\slash', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "back\\\\slash"}}}}'),
    ('line\nbreak', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "line\\nbreak"}}}}'),
    ('профиль', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "\\u043f\\u0440\\u043e\\u0444\\u0438\\u043b\\u044c"}}}}'),
    ('{literal}\tprofile', 'Object{spec: Object.spec{hostUsers: false, securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: "{literal}\\tprofile"}}}}'),
])
def test_user_namespace_policy_targets_only_plugin_pods(profile, expression):
    """The Fission workaround remaps IDs at pod creation only for labelled plugin pods."""
    manifest = user_namespace_policy(profile)
    assert (manifest['apiVersion'], manifest['kind']) == ('v1', 'List')
    policy, binding = manifest['items']
    assert (policy['apiVersion'], policy['kind']) == ('admissionregistration.k8s.io/v1', 'MutatingAdmissionPolicy')
    assert (binding['apiVersion'], binding['kind']) == ('admissionregistration.k8s.io/v1', 'MutatingAdmissionPolicyBinding')
    assert binding['spec'] == {'policyName': policy['metadata']['name']}
    assert policy['spec'] == {
        'matchConstraints': {
            'objectSelector': {'matchExpressions': [{'key': 'throngfission.io/installation', 'operator': 'Exists'}]},
            'resourceRules': [{'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['CREATE'], 'resources': ['pods']}],
        },
        'failurePolicy': 'Fail', 'reinvocationPolicy': 'IfNeeded',
        'mutations': [{'patchType': 'ApplyConfiguration', 'applyConfiguration': {'expression': expression}}],
    }
