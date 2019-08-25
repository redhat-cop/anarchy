#!/usr/bin/python

# Copyright: (c) 2018, Terry Jones <terry.jones@example.org>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

ANSIBLE_METADATA = {
    'metadata_version': '1.1',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: anarchy_schedule_action

short_description: Schedule action for AnarchySubject

version_added: "2.8"

description:
- "Schedule action for anarchy subject."

options:
  action:
    description:
    - Action name as defined by AnarchyGovernor
    required: true
  after:
    description:
    - Schedule action to run after this time interval
    required: false
  cancel:
    description:
    - Cancel scheduled actions
  datetime:
    description:
    - Datetime specification in ISO 8601 format (Ex: "2019-08-02T16:39:22Z")
    required: false

author:
- Johnathan Kupferer (jkupfere@redhat.com)
'''

EXAMPLES = '''
- name: Schedule deploy to run immediately
  anarchy_schedule_action:
    action: deploy

- name: Schedule idle to run immediately in 8 hours
  anarchy_schedule_action:
    action: idle
    after: 8h

- name: Schedule destroy to run at specific time
  anarchy_schedule_action:
    action: idle
    datetime: 2020-01-03T01:23:45Z

- name: Schedule destroy to run at specific time
  anarchy_schedule_action:
    action: idle
    cancel: true

- name: Clear actions
    clear: True
'''

RETURN = '''
action:
  description: The new AnarchyAction resource definition
  type: dict
  returned: always
canceled_actions:
  description: List of actions canceled
'''

import datetime
import kubernetes
import os
import re
import uuid
import yaml

from ansible.module_utils.basic import AnsibleModule

api_client = None
custom_objects_api = None
operator_domain = None
operator_namespace = None
datetime_re = re.compile(r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
time_interval_re = re.compile(r'^\d+[dhms]?$')

def time_to_seconds(time):
    if isinstance(time, int):
        return time
    if isinstance(time, str):
        if time.endswith('s'):
            return int(time[:-1])
        elif time.endswith('m'):
            return int(time[:-1]) * 60
        elif time.endswith('h'):
            return int(time[:-1]) * 3600
        elif time.endswith('d'):
            return int(time[:-1]) * 86400
        else:
            int(time)
    else:
        raise Exception("Invalid type for time, %s, must be int or str" % (type(time).__name__))

def create_action(governor, subject, action, after=None):
    if after:
        if time_interval_re.match(after):
            after = (
                datetime.datetime.utcnow() +
                datetime.timedelta(0, after_seconds)
            ).strftime('%FT%TZ')
    else:
        after = datetime.datetime.utcnow().strftime('%FT%TZ')
    return custom_objects_api.create_namespaced_custom_object(
        operator_domain,
        'v1',
        subject['metadata']['namespace'],
        'anarchyactions',
        {
            "apiVersion": operator_domain + "/v1",
            "kind": "AnarchyAction",
            "metadata": {
                "generateName": "%s-%s-" % (subject['metadata']['name'], action),
                "labels": {
                    operator_domain + '/action': action,
                    operator_domain + "/subject": subject['metadata']['name'],
                    operator_domain + "/governor": governor['metadata']['name'],
                },
                "ownerReferences": [{
                    "apiVersion": operator_domain + "/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": subject['metadata']['name'],
                    "uid": subject['metadata']['uid']
                }]
            },
            "spec": {
                "action": action,
                "after": after,
                "callbackToken": uuid.uuid4().hex,
                "governorRef": {
                    "apiVersion": operator_domain + "/v1",
                    "kind": "AnarchyGovernor",
                    "name": governor['metadata']['name'],
                    "namespace":  governor['metadata']['namespace'],
                    "uid": governor['metadata']['uid']
                },
                "subjectRef": {
                    "apiVersion": operator_domain + "/v1",
                    "kind": "AnarchySubject",
                    "name": subject['metadata']['name'],
                    "namespace":  subject['metadata']['namespace'],
                    "uid": subject['metadata']['uid']
                }
            }
        }
    )

def delete_action(action):
    return custom_objects_api.get_namespaced_custom_object(
        operator_domain,
        'v1',
        action['metadata']['namespace'],
        'anarchyactions',
        action['metadata']['name']
    )

def find_actions(subject, actions):
    return [
        resource for resource in custom_objects_api.list_namespaced_custom_object(
            operator_domain,
            'v1',
            subject['metadata']['namespace'],
            'anarchyactions',
            label_selector='%s/subject=%s' % (operator_domain, subject['metadata']['name'])
        ).get('items', []) if resource['spec']['action'] in actions
    ]

def get_governor(subject):
    '''Get governor from variables, then fetch latest state.'''
    return custom_objects_api.get_namespaced_custom_object(
        operator_domain,
        'v1',
        operator_namespace,
        'anarchygovernors',
        subject['spec']['governor']
    )

def get_subject():
    '''Get subject from variables, then fetch latest state.'''
    vars_subject = yaml.safe_load(os.environ.get('VARS', '{}')).get('anarchy_subject', {})
    return custom_objects_api.get_namespaced_custom_object(
        operator_domain,
        'v1',
        vars_subject['metadata']['namespace'],
        'anarchysubjects',
        vars_subject['metadata']['name']
    )

def init_kube_api():
    global custom_objects_api, api_client, operator_domain, operator_namespace
    kubernetes.config.load_kube_config()
    api_client = kubernetes.client.ApiClient()
    custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
    operator_domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
    operator_namespace = open('/run/secrets/kubernetes.io/serviceaccount/namespace').read()

def run_module():
    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        action=dict(type='str', required=False, default=None),
        after=dict(type='str', required=False, default=None),
        cancel=dict(type='list', required=False, default=[]),
        datetime=dict(type='str', required=False, default=None)
    )

    init_kube_api()
    subject = get_subject()
    governor = get_governor(subject)

    result = dict(
        action=None,
        canceled_actions=[],
        changed=False
    )

    module = AnsibleModule(
        argument_spec=module_args,
        mutually_exclusive=[
            ['after', 'datetime'],
            ['cancel', 'action'],
            ['cancel', 'after'],
            ['cancel', 'datetime']
        ],
        required_one_of=[
            ['action', 'cancel']
        ],
        supports_check_mode=True
    )

    if module.params['after'] \
    and not datetime_re.match(module.params['after']) \
    and not time_interval_re.match(module.params['after']):
        module.fail_json(msg="nvalid value for `after`: %s" % (after))

    result['canceled_actions'] = find_actions(
        subject,
        module.params['cancel'] if module.params['cancel'] else [module.params['action']]
    )

    if module.check_mode:
        module.exit_json(**result)

    if module.params['action']:
        result['action'] = create_action(
            governor=governor,
            subject=subject,
            action=module.params['action'],
            after=module.params['after']
        )

    for action in result['canceled_actions']:
        delete_action(action)

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
