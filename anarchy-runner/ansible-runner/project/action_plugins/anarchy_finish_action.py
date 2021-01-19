#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import re
import requests

from ansible.plugins.action import ActionBase
from ansible.module_utils.parsing.convert_bool import boolean

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_action_name = task_vars.get('anarchy_action_name')
        anarchy_subject_name = task_vars['anarchy_subject_name']
        anarchy_url = task_vars['anarchy_url']
        anarchy_run_pod_name = task_vars['anarchy_run_pod_name']
        anarchy_runner_name = task_vars['anarchy_runner_name']
        anarchy_runner_token = task_vars['anarchy_runner_token']

        finished_state = module_args.get('state', 'successful')

        if not anarchy_action_name:
            return dict(
                failed = True,
                msg = 'Run not executing for an action!'
            )

        response = requests.patch(
            anarchy_url + '/run/subject/' + anarchy_subject_name + '/actions/' + anarchy_action_name,
            headers={'Authorization': 'Bearer {}:{}:{}'.format(
                anarchy_runner_name, anarchy_run_pod_name, anarchy_runner_token
            )},
            json={finished_state: True}
        )
        result['action'] = response.json()['result']
        result['failed'] = not response.json()['success']

        return result
