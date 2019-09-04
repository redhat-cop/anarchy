#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
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

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_event = self._templar.template(task_vars['anarchy_event'], fail_on_undefined=True)
        anarchy_event_meta = anarchy_event['metadata']
        anarchy_event_name = anarchy_event_meta['name']

        try:
            if 'metadata' or 'spec' in module_args:
                patch = {}
                if 'metadata' in module_args:
                    patch['metadata'] = module_args['metadata']
                if 'spec' in module_args:
                    patch['spec'] = module_args['spec']
                result['anarchy_event'] = custom_objects_api.patch_namespaced_custom_object(
                    operator_domain, 'v1', operator_namespace, 'anarchyevents', anarchy_event_name, patch
                )
            if 'status' in module_args:
                patch = {'status': module_args['status']}
                result['anarchy_event'] = custom_objects_api.patch_namespaced_custom_object_status(
                    operator_domain, 'v1', operator_namespace, 'anarchyevents', anarchy_event_name, patch
                )
        except kubernetes.client.rest.ApiException as e:
            # Some event handles remove the subject and so may remove the event as well
            if e.status != 404:
                raise

        return result