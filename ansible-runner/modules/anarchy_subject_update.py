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
module: anarchy_subject_update

short_description: Update AnarchySubject

version_added: "2.8"

description:
- "Apply updates to current AnarchySubject"

options:
  metadata:
    description:
    - Updates to apply to metadata
    required: false
  spec:
    description:
    - Updates to apply to spec
    required: false
  status:
    description:
    - Updates to apply to status
    required: false

author:
- Johnathan Kupferer (jkupfere@redhat.com)
'''

EXAMPLES = '''
- name: Set state started in subject status
  anarchy_subject_update:
    status:
      state: started
'''

RETURN = '''
subject:
  description: The updated AnarchySubject resource definition
  type: dict
  returned: always
'''

import kubernetes
import jsonpatch
import os
import yaml

from ansible.module_utils.basic import AnsibleModule

api_client = None
custom_objects_api = None
operator_domain = None

def update_patch_filter(update, item):
    path = item['path']
    op = item['op']
    if op == 'remove':
        return False
    field = path.split('/',2)[1]
    return field in update and update[field]

def update_to_patch(subject, update):
    return [
        item for item in jsonpatch.JsonPatch.from_diff(
            subject,
            update
        ) if update_patch_filter(update, item)
    ]

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

def patch_subject(subject, patch):
    return custom_objects_api.patch_namespaced_custom_object(
        operator_domain,
        'v1',
        subject['metadata']['namespace'],
        'anarchysubjects',
        subject['metadata']['name'],
        patch
    )

def patch_subject_status(subject, patch):
    return custom_objects_api.patch_namespaced_custom_object_status(
        operator_domain,
        'v1',
        subject['metadata']['namespace'],
        'anarchysubjects',
        subject['metadata']['name'],
        patch
    )

def init_kube_api():
    global custom_objects_api, api_client, operator_domain
    kubernetes.config.load_kube_config()
    api_client = kubernetes.client.ApiClient()
    custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
    operator_domain = os.environ.get('OPERATOR_DOMAIN', 'gpte.redhat.com')

def run_module():
    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        metadata=dict(type='dict', required=False),
        spec=dict(type='dict', required=False),
        status=dict(type='dict', required=False)
    )

    init_kube_api()
    subject = get_subject()

    result = dict(
        changed=False,
        subject=subject
    )

    module = AnsibleModule(
        argument_spec=module_args,
        required_one_of=[
            ['metadata', 'spec', 'status']
        ],
        supports_check_mode=True
    )

    resource_patch = update_to_patch(
        subject=subject,
        update={
            "metadata": module.params['metadata'],
            "spec": module.params['spec']
        }
    )

    status_patch = update_to_patch(
        subject=subject,
        update={
            "status": module.params['status']
        }
    )

    if resource_patch or status_patch:
        result['changed'] = True

    if module.check_mode:
        module.exit_json(**result)

    # Hack to force json-patch
    custom_objects_api.api_client.select_header_content_type = lambda _ : 'application/json-patch+json'

    if resource_patch:
        subject = patch_subject(subject, resource_patch)

    if status_patch:
        subject = patch_subject_status(subject, status_patch)

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
