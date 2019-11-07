#!/usr/bin/python

# Copyright: (c) 2018, Terry Jones <terry.jones@example.org>
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

        remove_finalizers = module_args['remove_finalizers']
        if isinstance(remove_finalizers, str):
            if re.match('[TtYy]'):
                remove_finalizers = True
            else:
                remove_finalizers = False

        response = requests.delete(
            anarchy_url + '/run/subject/' + anarchy_subject['name'],
            headers={'Authorization': 'Bearer {}:{}:{}'.format(
                anarchy_runner_name, anarchy_run_pod_name, anarchy_runner_token
            )},
            json=dict(remove_finalizers=remove_finalizers)
        )
        result['subject'] = response.json()['result']
        result['failed'] = not response.json()['success']

        return result
