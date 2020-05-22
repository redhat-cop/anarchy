#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

import datetime
import os
import re
import requests

from ansible.plugins.action import ActionBase

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

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None, **_):
        result = super(ActionModule, self).run(tmp, task_vars)
        module_args = self._task.args.copy()
        anarchy_subject_name = task_vars['anarchy_subject_name']
        anarchy_url = task_vars['anarchy_url']
        anarchy_run_pod_name = task_vars['anarchy_run_pod_name']
        anarchy_runner_name = task_vars['anarchy_runner_name']
        anarchy_runner_token = task_vars['anarchy_runner_token']

        action = module_args.get('action', None)
        after = module_args.get('after', None)
        cancel = module_args.get('cancel', [])

        if isinstance(after, datetime.datetime):
            after = after.strftime('%FT%TZ')
        elif not after:
            after = datetime.datetime.utcnow().strftime('%FT%TZ')
        elif time_interval_re.match(after):
            after = (
                datetime.datetime.utcnow() +
                datetime.timedelta(0, time_interval_to_seconds(after))
            ).strftime('%FT%TZ')
        elif datetime_re.match(after):
            pass
        else:
            result['failed'] = True
            result['message'] = 'Invalid value for `after`: {}'.format(after)
            return result

        response = requests.post(
            anarchy_url + '/run/subject/' + anarchy_subject_name + '/actions',
            headers={'Authorization': 'Bearer {}:{}:{}'.format(
                anarchy_runner_name, anarchy_run_pod_name, anarchy_runner_token
            )},
            json=dict(action=action, after=after, cancel=cancel)
        )

        result['action'] = response.json()['result']
        result['failed'] = not response.json()['success']

        return result
