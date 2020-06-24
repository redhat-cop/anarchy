#!/usr/bin/python

# Copyright: (c) 2019, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from datetime import datetime, timedelta
import os
import re
import requests

from ansible.plugins.action import ActionBase

datetime_re = re.compile(r'^\d\d\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

def parse_time_interval(interval):
    if isinstance(interval, int):
        return timedelta(seconds=interval)
    if isinstance(interval, str) \
    and interval != '':
        m = re.match(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$', interval)
        if m:
            return timedelta(
                days=int(m.group(1)),
                hours=int(m.group(2)),
                minutes=int(m.group(3)),
                seconds=int(m.group(4))
            )
        else:
            return None
    return None

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

        if isinstance(after, datetime):
            after = after.strftime('%FT%TZ')
        elif not after:
            after = datetime.utcnow().strftime('%FT%TZ')
        elif datetime_re.match(after):
            pass
        else:
            interval = parse_time_interval(after)
            if interval:
                after = (datetime.utcnow() + interval).strftime('%FT%TZ')
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
