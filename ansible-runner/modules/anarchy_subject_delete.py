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
module: anarchy_subject_delete

short_description: Delete AnarchySubject

version_added: "2.8"

description:
- "Delete current AnarchySubject"

options:
  remove_finalizers:
    description:
    - Boolean to indicate whether finalizers should be removed
    required: false

author:
- Johnathan Kupferer (jkupfere@redhat.com)
'''

EXAMPLES = '''
- name: Delete subject, removing finalizers
  anarchy_subject_delete:
    remove_finalizers: true
'''

RETURN = '''
subject:
  description: The deleted AnarchySubject resource definition
  type: dict
  returned: always
'''

import kubernetes
import os
import yaml

from ansible.module_utils.basic import AnsibleModule

api_client = None
custom_objects_api = None
operator_domain = None

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

def remove_finalizers(subject):
    if 'finalizers' not in subject['metadata']:
        return subject

    return custom_objects_api.patch_namespaced_custom_object(
        operator_domain,
        'v1',
        subject['metadata']['namespace'],
        'anarchysubjects',
        subject['metadata']['name'],
        { "metadata": { "finalizers": None } }
    )

def delete_subject(subject):
    if 'deletionTimestamp' in subject['metadata']:
        return subject

    delete_options = kubernetes.client.V1DeleteOptions()
    return custom_objects_api.delete_namespaced_custom_object(
        operator_domain,
        'v1',
        subject['metadata']['namespace'],
        'anarchysubjects',
        subject['metadata']['name'],
        delete_options
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
        remove_finalizers=dict(type='bool', required=False, default=False)
    )

    init_kube_api()
    subject = get_subject()

    result = dict(
        changed=True,
        subject=subject
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    if not module.params['remove_finalizers'] and 'deletionTimestamp' in subject['metadata']:
        result['changed'] = False

    if module.check_mode:
        module.exit_json(**result)

    result['subject'] = delete_subject(subject)

    # Hack to force merge-patch
    custom_objects_api.api_client.select_header_content_type = lambda _ : 'application/merge-patch+json'

    remove_finalizers(subject)

    module.exit_json(**result)

def main():
    run_module()

if __name__ == '__main__':
    main()
