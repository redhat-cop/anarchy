#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import yaml

from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)

        anarchy_action_name = task_vars.get('anarchy_action_name')
        if not anarchy_action_name:
            return dict(
                failed = True,
                msg = 'Run not executing for an action!'
            )

        module_args = self._task.args.copy()
        finished_state = module_args.get('state', 'successful')

        anarchy_output_dir = task_vars['anarchy_output_dir']
        anarchy_result_path = os.path.join(anarchy_output_dir, 'anarchy-result.yaml')
        if os.path.exists(anarchy_result_path):
            with open(anarchy_result_path) as f:
                result_data = yaml.safe_load(f)
        else:
            result_data = {}

        result_data['finishAction'] = {
            "state": str(finished_state),
        }

        with open(os.path.join(anarchy_output_dir, 'anarchy-result.yaml'), 'w') as f:
            yaml.safe_dump(result_data, f)

        result['failed'] = False
        return result
