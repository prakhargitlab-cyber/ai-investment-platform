"""Offline generator only. Reads captured JSON; prints a pod; never calls Kubernetes."""
import argparse
import copy
import json
import re

DB_ENV = {
    'AIP_RESEARCH_DATABASE_BACKEND', 'AIP_RESEARCH_DATABASE_HOST',
    'AIP_RESEARCH_DATABASE_PORT', 'AIP_RESEARCH_DATABASE_NAME',
    'AIP_RESEARCH_DATABASE_USER', 'AIP_RESEARCH_DATABASE_PASSWORD',
    'AIP_RESEARCH_DATABASE_SCHEMA', 'AIP_RESEARCH_DATABASE_SSL_MODE',
    'AIP_RESEARCH_DATABASE_CONNECT_TIMEOUT_SECONDS',
    'AIP_RESEARCH_DATABASE_STATEMENT_TIMEOUT_SECONDS',
}

def selected(labels, selector):
    if not selector:
        raise ValueError('Deployment selector must be captured')
    matches = all(labels.get(k) == v for k, v in selector.get('matchLabels', {}).items())
    for expression in selector.get('matchExpressions', []):
        key, operator = expression['key'], expression['operator']
        values = expression.get('values', [])
        if operator == 'In':
            matches &= key in labels and labels[key] in values
        elif operator == 'NotIn':
            matches &= key not in labels or labels[key] not in values
        elif operator == 'Exists':
            matches &= key in labels
        elif operator == 'DoesNotExist':
            matches &= key not in labels
        else:
            raise ValueError('Unknown selector operator')
    return matches

def generate(deployment, services, image):
    if not re.fullmatch(r'ai-investment-registry:5000/ai-investment/research-engine@sha256:[0-9a-f]{64}', image):
        raise ValueError('Approved immutable research image required')
    if deployment['metadata']['name'] != 'research-engine' or deployment['metadata']['namespace'] != 'ai-investment':
        raise ValueError('Unexpected deployment')
    containers = deployment['spec']['template']['spec']['containers']
    if len(containers) != 1 or containers[0]['name'] != 'research-engine':
        raise ValueError('Unexpected container layout')
    source = containers[0]
    if source.get('envFrom'):
        raise ValueError('envFrom must be resolved and reviewed explicitly')
    env = [copy.deepcopy(e) for e in source.get('env', []) if e['name'] in DB_ENV]
    if len({e['name'] for e in env}) != len(env):
        raise ValueError('Duplicate database environment')
    for e in env:
        if 'valueFrom' in e:
            refs = e['valueFrom']
            if len(refs) != 1 or not set(refs) <= {'secretKeyRef', 'configMapKeyRef'}:
                raise ValueError('Only reviewed secret/configmap references supported')
    password = next((e for e in env if e['name'] == 'AIP_RESEARCH_DATABASE_PASSWORD'), {})
    ref = password.get('valueFrom', {}).get('secretKeyRef', {})
    if 'value' in password or not ref.get('name') or not ref.get('key') or ref.get('optional'):
        raise ValueError('Database password must be a required Secret reference, never literal/configmap')
    values = {e['name']: e.get('value') for e in env}
    for key, value in [('BACKEND', 'postgres'), ('SCHEMA', 'research'), ('NAME', 'investment')]:
        if values.get('AIP_RESEARCH_DATABASE_' + key) != value:
            raise ValueError('Explicit production database identity required')
    if not values.get('AIP_RESEARCH_DATABASE_HOST') or 'AIP_RESEARCH_DATABASE_USER' not in values:
        raise ValueError('Explicit host/user required')
    labels = {'maintenance.aip/purpose': 'refresh-observation'}
    if selected(labels, deployment['spec'].get('selector')):
        raise ValueError('Observer would match the Deployment selector')
    for service in services['items']:
        selector = service.get('spec', {}).get('selector')
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            raise ValueError('Observer would be selected by a Service')
    env += [{'name': 'PYTHONDONTWRITEBYTECODE', 'value': '1'},
            {'name': 'AIP_RESEARCH_OPPORTUNITY_RECOVERY_PAUSED', 'value': 'true'},
            {'name': 'AIP_RESEARCH_OPPORTUNITY_SCHEDULER_ENABLED', 'value': 'false'}]
    return {'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': 'research-maintenance-observer', 'namespace': 'ai-investment', 'labels': labels},
            'spec': {'restartPolicy': 'Never', 'activeDeadlineSeconds': 14400,
                     'automountServiceAccountToken': False, 'enableServiceLinks': False,
                     'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                     'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                                         'seccompProfile': {'type': 'RuntimeDefault'}},
                     'containers': [{'name': 'research-engine', 'image': image, 'imagePullPolicy': 'Always',
                        'command': ['/usr/local/bin/python', '-I', '-S', '-B', '-c'],
                        'args': ['import time; time.sleep(14400)'], 'env': env,
                        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                            'capabilities': {'drop': ['ALL']}},
                        'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'},
                                      'limits': {'cpu': '500m', 'memory': '512Mi'}}}]}}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deployment', required=True)
    parser.add_argument('--services', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    def read(path):
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    print(json.dumps(generate(read(args.deployment), read(args.services), args.image), indent=2))
