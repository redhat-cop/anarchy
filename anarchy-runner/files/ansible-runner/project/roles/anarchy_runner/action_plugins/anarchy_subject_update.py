#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import re
import requests

from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_subject = task_vars['anarchy_subject']
        anarchy_url = task_vars['anarchy_url']
        anarchy_run_pod_name = task_vars['anarchy_run_pod_name']
        anarchy_runner_name = task_vars['anarchy_runner_name']
        anarchy_runner_token = task_vars['anarchy_runner_token']

        patch = {}
        if 'metadata' in module_args:
            patch['metadata'] = module_args['metadata']
        if 'spec' in module_args:
            patch['spec'] = module_args['spec']
        if 'status' in module_args:
            patch['status'] = module_args['status']

        response = requests.patch(
            anarchy_url + '/run/subject/' + anarchy_subject['name'],
            headers={'Authorization': 'Bearer {}:{}:{}'.format(
                anarchy_runner_name, anarchy_run_pod_name, anarchy_runner_token
            )},
            json=dict(patch=patch)
        )
        result['subject'] = response.json()['result']
        result['failed'] = not response.json()['success']

        return result
