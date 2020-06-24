# Copyright (c) 2020 Red Hat
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible.errors import AnsibleFilterError
from ansible.module_utils.six.moves.urllib.parse import urlsplit
from ansible.utils import helpers

from datetime import datetime, timedelta

import re

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
                seconds=int(m.group(4) or 0),
            )
        else:
            return None
    return None

def anarchy_after_datetime(value, query='', alias='urlsplit'):
    td = parse_time_interval(value)
    if not td:
        raise AnsibleFilterError('Invalid time interval: %s' % value)
    return datetime.utcnow() + td

def anarchy_after_timestamp(value, query='', alias='urlsplit'):
    return anarchy_after_datetime(value).strftime('%FT%TZ')

# ---- Ansible filters ----
class FilterModule(object):
    ''' URI filter '''

    def filters(self):
        return {
            'anarchy_after_datetime': anarchy_after_datetime,
            'anarchy_after_timestamp': anarchy_after_timestamp,
        }
