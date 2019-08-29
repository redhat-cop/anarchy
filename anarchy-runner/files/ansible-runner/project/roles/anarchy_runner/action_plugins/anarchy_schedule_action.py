#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import datetime
import kubernetes
import os
import re
import uuid

from ansible.plugins.action import ActionBase

kubernetes.config.load_kube_config()
api_client = kubernetes.client.ApiClient()
custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
operator_domain = os.environ.get('OPERATOR_DOMAIN', 'anarchy.gpte.redhat.com')
operator_namespace = 'anarchy-operator'
if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
    with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as namespace_fh:
        operator_namespace = namespace_fh.read()

datetime_re = re.compile(r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
time_interval_re = re.compile(r'^\d+[dhms]?$')

def time_interval_to_seconds(interval):
    if isinstance(interval, int):
        return interval
    if isinstance(interval, str):
        if interval.endswith('s'):
            return int(interval[:-1])
        elif interval.endswith('m'):
            return int(interval[:-1]) * 60
        elif interval.endswith('h'):
            return int(interval[:-1]) * 3600
        elif interval.endswith('d'):
            return int(interval[:-1]) * 86400
        else:
            int(interval)
    else:
        raise Exception("Invalid type for time interval, %s, must be int or str" % (type(interval).__name__))

def create_action(anarchy_governor, anarchy_subject, action, after=None):
    if after:
        if time_interval_re.match(after):
            after = (
                datetime.datetime.utcnow() +
                datetime.timedelta(0, time_interval_to_seconds(after))
            ).strftime('%FT%TZ')
    else:
        after = datetime.datetime.utcnow().strftime('%FT%TZ')
    return custom_objects_api.create_namespaced_custom_object(
        operator_domain,
        'v1',
        anarchy_subject['metadata']['namespace'],
        'anarchyactions',
        {
            "apiVersion": operator_domain + "/v1",
            "kind": "AnarchyAction",
            "metadata": {
                "generateName": "%s-%s-" % (anarchy_subject['metadata']['name'], action),
                "labels": {
                    operator_domain + '/action': action,
                    operator_domain + "/subject": anarchy_subject['metadata']['name'],
                    operator_domain + "/governor": anarchy_governor['metadata']['name'],
                },
                "ownerReferences": [{
                    "apiVersion": operator_domain + "/v1",
                    "controller": True,
                    "kind": "AnarchySubject",
                    "name": anarchy_subject['metadata']['name'],
                    "uid": anarchy_subject['metadata']['uid']
                }]
            },
            "spec": {
                "action": action,
                "after": after,
                "callbackToken": uuid.uuid4().hex,
                "governorRef": {
                    "apiVersion": operator_domain + "/v1",
                    "kind": "AnarchyGovernor",
                    "name": anarchy_governor['metadata']['name'],
                    "namespace":  anarchy_governor['metadata']['namespace'],
                    "uid": anarchy_governor['metadata']['uid']
                },
                "subjectRef": {
                    "apiVersion": operator_domain + "/v1",
                    "kind": "AnarchySubject",
                    "name": anarchy_subject['metadata']['name'],
                    "namespace":  anarchy_subject['metadata']['namespace'],
                    "uid": anarchy_subject['metadata']['uid']
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

def find_actions(anarchy_subject, actions):
    return [
        resource for resource in custom_objects_api.list_namespaced_custom_object(
            operator_domain,
            'v1',
            anarchy_subject['metadata']['namespace'],
            'anarchyactions',
            label_selector='%s/subject=%s' % (operator_domain, anarchy_subject['metadata']['name'])
        ).get('items', []) if resource['spec']['action'] in actions
    ]

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_governor = self._templar.template(task_vars['anarchy_governor'], fail_on_undefined=True)
        anarchy_subject = self._templar.template(task_vars['anarchy_subject'], fail_on_undefined=True)

        action = module_args.get('action', None)
        after = module_args.get('after', None)

        if after \
        and not datetime_re.match(after) \
        and not time_interval_re.match(after):
            result['failed'] = True
            result['message'] = 'Invalid value for `after`: {}'.format(after)
            return result

        for action_obj in find_actions(anarchy_subject, module_args.get('cancel', [action])):
            delete_action(action_obj)

        if action:
            result['action'] = create_action(
                anarchy_governor=anarchy_governor,
                anarchy_subject=anarchy_subject,
                action=action,
                after=after
            )

        return result
