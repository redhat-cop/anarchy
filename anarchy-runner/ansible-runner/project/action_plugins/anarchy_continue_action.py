#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import os
import re
import yaml

from ansible.plugins.action import ActionBase
from datetime import datetime, timedelta

datetime_re = re.compile(r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

def parse_time_interval(interval):
    if isinstance(interval, int):
        return timedelta(seconds=interval)
    if isinstance(interval, str) \
    and interval != '':
        m = re.match(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$', interval)
        if m:
            return timedelta(
                days=int(m.group(1) or 0),
                hours=int(m.group(2) or 0),
                minutes=int(m.group(3) or 0),
                seconds=int(m.group(4) or 0)
            )
        else:
            return None
    return None

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
        after = module_args.get('after', None)
        vars = module_args.get('vars', {})

        if not after:
            return dict(
                failed = True,
                message = 'Parameter `after` is required',
            )

        interval = parse_time_interval(after)
        if not interval:
            return dict(
                failed = True,
                message = 'Parameter `after` must be time interval. Ex: 1h, 10m, 30s',
            )

        after_timestamp = (datetime.utcnow() + interval).strftime('%FT%TZ')

        anarchy_output_dir = task_vars['anarchy_output_dir']
        anarchy_result_path = os.path.join(anarchy_output_dir, 'anarchy-result.yaml')

        if os.path.exists(anarchy_result_path):
            with open(anarchy_result_path) as f:
                result_data = yaml.safe_load(f)
        else:
            result_data = {}

        result_data['continueAction'] = {
            "after": after_timestamp,
            "vars": vars,
        }

        with open(os.path.join(anarchy_output_dir, 'anarchy-result.yaml'), 'w') as f:
            yaml.safe_dump(result_data, f)

        result['failed'] = False
        return result
