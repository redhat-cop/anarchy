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
        anarchy_output_dir = task_vars['anarchy_output_dir']
        module_args = self._task.args.copy()
        after = module_args.get('after', None)

        if not after:
            return dict(
                failed = True,
                message = 'Parameter `after` is required',
            )

        interval = parse_time_interval(after)
        if interval:
            after = (datetime.utcnow() + interval).strftime('%FT%TZ')
        else:
            return dict(
                failed = True,
                message = 'Parameter `after` must be time interval. Ex: 1h, 10m, 30s',
            )

        with open(os.path.join(anarchy_output_dir, 'continue.yaml'), 'w') as f:
            yaml.safe_dump({'after': int(interval.total_seconds())}, f)

        return dict(
            after = int(interval.total_seconds()),
            failed = False,
        )
