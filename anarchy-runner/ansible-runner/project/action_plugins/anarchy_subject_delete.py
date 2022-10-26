#!/usr/bin/python

# Copyright: (c) 2018, Terry Jones <terry.jones@example.org>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import yaml

from ansible.plugins.action import ActionBase
from ansible.module_utils.parsing.convert_bool import boolean

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)

        module_args = self._task.args.copy()
        remove_finalizers = boolean(module_args.get('remove_finalizers', False), strict=False)

        anarchy_output_dir = task_vars['anarchy_output_dir']
        anarchy_result_path = os.path.join(anarchy_output_dir, 'anarchy-result.yaml')

        if os.path.exists(anarchy_result_path):
            with open(anarchy_result_path) as f:
                result_data = yaml.safe_load(f)
        else:
            result_data = {}

        result_data['deleteSubject'] = {
            "removeFinalizers": remove_finalizers,
        }

        with open(os.path.join(anarchy_output_dir, 'anarchy-result.yaml'), 'w') as f:
            yaml.safe_dump(result_data, f)

        result['failed'] = False
        return result
