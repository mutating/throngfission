import base64
import json
from typing import Dict, List, Tuple

from throngfission.settings import Settings


def user_namespace_policy(apparmor_profile: str = '') -> Dict[str, object]:
    """Keep pod UID remapping when Fission omits hostUsers from its Deployment."""
    name = 'throngfission-user-namespaces'
    version = 'admissionregistration.k8s.io/v1'
    specification = 'hostUsers: false'
    if apparmor_profile:
        specification += ', securityContext: Object.spec.securityContext{appArmorProfile: Object.spec.securityContext.appArmorProfile{type: "Localhost", localhostProfile: ' + json.dumps(apparmor_profile) + '}}'
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': version, 'kind': 'MutatingAdmissionPolicy', 'metadata': {'name': name}, 'spec': {
            'matchConstraints': {
                'objectSelector': {'matchExpressions': [{'key': 'throngfission.io/installation', 'operator': 'Exists'}]},
                'resourceRules': [{'apiGroups': [''], 'apiVersions': ['v1'], 'operations': ['CREATE'], 'resources': ['pods']}],
            },
            'failurePolicy': 'Fail', 'reinvocationPolicy': 'IfNeeded',
            'mutations': [{'patchType': 'ApplyConfiguration', 'applyConfiguration': {'expression': 'Object{spec: Object.spec{' + specification + '}}'}}],
        }},
        {'apiVersion': version, 'kind': 'MutatingAdmissionPolicyBinding', 'metadata': {'name': name}, 'spec': {'policyName': name}},
    ]}


def resources(settings: Settings, secret: str) -> List[Tuple[str, Dict[str, object]]]:
    """Describe a shared runner and a bounded-history cleanup CronJob."""
    name, namespace = settings.function_name, settings.namespace
    podspec: Dict[str, object] = {
        'hostUsers': False,
        'terminationGracePeriodSeconds': settings.termination_grace_period,
        'containers': [
            {'name': name},
            {'name': 'fetcher', 'lifecycle': {'preStop': {'sleep': {'seconds': settings.termination_grace_period}}}},
        ],
        'volumes': [{'name': 'worker', 'configMap': {'name': name}}],
    }
    if settings.anti_affinity != 'none':
        term = {'topologyKey': 'kubernetes.io/hostname', 'labelSelector': {'matchLabels': {'environmentName': name}}}
        if settings.anti_affinity == 'soft':
            policy: Dict[str, object] = {'preferredDuringSchedulingIgnoredDuringExecution': [{'weight': 100, 'podAffinityTerm': term}]}
        else:
            policy = {'requiredDuringSchedulingIgnoredDuringExecution': [term]}
        podspec['affinity'] = {'podAntiAffinity': policy}
    custom = '/apis/fission.io/v1/namespaces/' + namespace + '/'
    core = '/api/v1/namespaces/' + namespace + '/'
    rbac = '/apis/rbac.authorization.k8s.io/v1/namespaces/' + namespace + '/'
    bootstrap = 'import sys\nsys.path.insert(0, "/throngfission/worker.zip")\nfrom throngfission.runtime import main\n'
    variables: List[Dict[str, object]] = [
        {'name': 'THRONGFISSION_SECRET', 'valueFrom': {'secretKeyRef': {'name': name, 'key': 'token'}}},
        {'name': 'THRONGFISSION_SESSION_TIMEOUT', 'value': str(settings.session_timeout)},
        {'name': 'THRONGFISSION_TRANSFER_CHUNK_SIZE', 'value': str(settings.transfer_chunk_size)},
        {'name': 'THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE', 'value': str(settings.websocket_max_message_size)},
    ]
    objects: List[Tuple[str, str, str, Dict[str, object]]] = [
        (core + 'secrets', 'v1', 'Secret', {'data': {'token': base64.b64encode(secret.encode()).decode()}}),
        (core + 'serviceaccounts', 'v1', 'ServiceAccount', {}),
        (rbac + 'roles', 'rbac.authorization.k8s.io/v1', 'Role', {'rules': [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'patch', 'delete']}]}),
        (rbac + 'rolebindings', 'rbac.authorization.k8s.io/v1', 'RoleBinding', {'subjects': [{'kind': 'ServiceAccount', 'name': name, 'namespace': namespace}], 'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name}}),
        (custom + 'environments', 'fission.io/v1', 'Environment', {'spec': {
            'version': 3, 'poolsize': settings.pool_size, 'terminationGracePeriod': settings.termination_grace_period,
            'resources': {'requests': {'cpu': settings.cpu_request, 'memory': settings.memory_request}},
            'runtime': {'image': settings.resolved_image, 'podspec': podspec, 'container': {'name': name, 'securityContext': {'runAsUser': 0, 'runAsGroup': 0, 'runAsNonRoot': False, 'privileged': False, 'allowPrivilegeEscalation': False, 'seccompProfile': {'type': 'RuntimeDefault'}}, 'volumeMounts': [{'name': 'worker', 'mountPath': '/throngfission', 'readOnly': True}], 'env': [*variables,
                {'name': 'WSGI_FRAMEWORK', 'value': 'GEVENT'}, {'name': 'MOCK_FETCHER', 'value': 'true'}, {'name': 'TIMEOUT', 'value': str(settings.socket_tracker_interval)},
                {'name': 'THRONGFISSION_POD_UID', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.uid'}}},
                {'name': 'THRONGFISSION_POD_NAME', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.name'}}},
            ]}},
        }}),
        (custom + 'packages', 'fission.io/v1', 'Package', {'spec': {'environment': {'name': name, 'namespace': namespace}, 'deployment': {'type': 'literal', 'literal': base64.b64encode(bootstrap.encode()).decode()}}}),
        (custom + 'functions', 'fission.io/v1', 'Function', {'spec': {
            'environment': {'name': name, 'namespace': namespace}, 'package': {'packageref': {'name': name, 'namespace': namespace}, 'functionName': 'main'},
            # Match Fission's Go serialization: finalizer updates must not change
            # the spec's generation and trigger replacement of a live worker.
            'InvokeStrategy': {'StrategyType': 'execution', 'ExecutionStrategy': {'ExecutorType': 'poolmgr', 'MinScale': 0, 'MaxScale': 0, 'SpecializationTimeout': 0, 'TargetCPUPercent': 0}},
            'resources': {}, 'onceOnly': True, 'requestsPerPod': 1, 'idletimeout': settings.function_idle_timeout,
            'streaming': {'protocol': 'websocket', 'idleTimeoutSeconds': settings.stream_idle_timeout, **({'maxDurationSeconds': settings.stream_max_duration} if settings.stream_max_duration else {})},
        }}),
        (custom + 'httptriggers', 'fission.io/v1', 'HTTPTrigger', {'spec': {'relativeurl': '/throngfission/' + namespace + '/' + name, 'methods': ['GET'], 'functionref': {'type': 'name', 'name': name}, 'host': '', 'createingress': False}}),
        ('/apis/networking.k8s.io/v1/namespaces/' + namespace + '/networkpolicies', 'networking.k8s.io/v1', 'NetworkPolicy', {'spec': {
            'podSelector': {'matchLabels': {'throngfission.io/installation': name}, 'matchExpressions': [{'key': 'executorType', 'operator': 'In', 'values': ['poolmgr']}]},
            'policyTypes': ['Ingress'],
            'ingress': [
                {'from': [{'namespaceSelector': {}, 'podSelector': {'matchLabels': {'svc': 'executor'}}}], 'ports': [{'protocol': 'TCP', 'port': 8000}, {'protocol': 'TCP', 'port': 8888}]},
                {'from': [{'namespaceSelector': {}, 'podSelector': {'matchLabels': {'application': 'fission-router', 'svc': 'router'}}}], 'ports': [{'protocol': 'TCP', 'port': 8888}]},
                {'from': [{'podSelector': {'matchLabels': {'throngfission.io/installation': name, 'throngfission.io/role': 'janitor'}}}], 'ports': [{'protocol': 'TCP', 'port': 8888}]},
            ],
        }}),
        ('/apis/batch/v1/namespaces/' + namespace + '/cronjobs', 'batch/v1', 'CronJob', {'spec': {
            'schedule': settings.janitor_schedule, 'concurrencyPolicy': 'Forbid', 'successfulJobsHistoryLimit': settings.janitor_success_history, 'failedJobsHistoryLimit': settings.janitor_failure_history,
            'jobTemplate': {'spec': {'ttlSecondsAfterFinished': settings.janitor_job_ttl, 'backoffLimit': settings.janitor_backoff_limit, 'template': {'metadata': {'labels': {'throngfission.io/installation': name, 'throngfission.io/role': 'janitor'}}, 'spec': {
                'serviceAccountName': name, 'hostUsers': False, 'restartPolicy': 'Never', 'terminationGracePeriodSeconds': settings.termination_grace_period,
                'containers': [{'name': 'janitor', 'image': settings.resolved_image, 'command': ['python', '-c', 'import sys; sys.path.insert(0, "/janitor/worker.zip"); from throngfission.janitor import main; main()'],
                                'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'runAsNonRoot': True, 'privileged': False, 'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}, 'seccompProfile': {'type': 'RuntimeDefault'}},
                                'env': [*variables, {'name': 'THRONGFISSION_NAMESPACE', 'value': namespace}, {'name': 'THRONGFISSION_NAME', 'value': name},
                                        {'name': 'THRONGFISSION_API_RECOVERY_TIMEOUT', 'value': str(settings.api_recovery_timeout)},
                                        {'name': 'THRONGFISSION_POLL_INTERVAL', 'value': str(settings.poll_interval)},
                                        {'name': 'THRONGFISSION_JANITOR_REQUEST_TIMEOUT', 'value': str(settings.janitor_request_timeout)},
                                        {'name': 'THRONGFISSION_JANITOR_UNREACHABLE_TIMEOUT', 'value': str(settings.janitor_unreachable_timeout)}],
                                'resources': {'requests': {'cpu': settings.janitor_cpu_request, 'memory': settings.janitor_memory_request}},
                                'volumeMounts': [{'name': 'janitor', 'mountPath': '/janitor', 'readOnly': True}]}],
                'volumes': [{'name': 'janitor', 'configMap': {'name': name}}],
            }}}},
        }}),
    ]
    return [(path, {'apiVersion': version, 'kind': kind, 'metadata': {'name': name, 'namespace': namespace, 'labels': {'throngfission.io/installation': name}}, **body}) for path, version, kind, body in objects]


def root(settings: Settings, fingerprint: str, archive: str) -> Dict[str, object]:
    return {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': settings.function_name, 'namespace': settings.namespace, 'labels': {'throngfission.io/installation': settings.function_name}}, 'data': {'fingerprint': fingerprint}, 'binaryData': {'worker.zip': archive}}
