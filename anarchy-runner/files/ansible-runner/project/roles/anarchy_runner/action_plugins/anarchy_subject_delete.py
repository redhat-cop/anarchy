#!/usr/bin/python

# Copyright: (c) 2018, Terry Jones <terry.jones@example.org>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import kubernetes
import os

from ansible.plugins.action import ActionBase

kubernetes.config.load_kube_config()
api_client = kubernetes.client.ApiClient()
custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
operator_domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
operator_namespace = 'anarchy-operator'
if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as namespace_fh:
        operator_namespace = namespace_fh.read()

def remove_finalizers(anarchy_subject):
    if 'finalizers' not in anarchy_subject['metadata']:
        return anarchy_subject

    return custom_objects_api.patch_namespaced_custom_object(
        operator_domain, 'v1', anarchy_subject['metadata']['namespace'],
        'anarchysubjects', anarchy_subject['metadata']['name'],
        { "metadata": { "finalizers": None } }
    )

def delete_subject(anarchy_subject):
    if 'deletionTimestamp' in anarchy_subject['metadata']:
        return anarchy_subject

    return custom_objects_api.delete_namespaced_custom_object(
        operator_domain, 'v1', anarchy_subject['metadata']['namespace'],
        'anarchysubjects', anarchy_subject['metadata']['name'],
        kubernetes.client.V1DeleteOptions()
    )

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_subject = task_vars['anarchy_subject']

        result['anarchy_subject'] = delete_subject(anarchy_subject)
        if module_args['remove_finalizers']:
            result['anarchy_subject'] = remove_finalizers(anarchy_subject)

        return result
