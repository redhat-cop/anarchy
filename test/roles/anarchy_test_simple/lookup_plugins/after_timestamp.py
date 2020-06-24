# python 3 headers, required if submitting to Ansible
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = """
lookup: after_timestamp
author: Johnathan Kupferer <jkupfere@redhat.com>
version_added: "2.9"
short_description: Return UTC timestamp adjusted for provided interval.
description:
  - This lookup returns the contents from a file on the Ansible controller's file system.
options:
  _terms:
    description: Duration string.
    required: True
"""
from ansible.errors import AnsibleError, AnsibleParserError
from ansible.plugins.lookup import LookupBase
from ansible.utils.display import Display

from datetime import datetime, timedelta

display = Display()

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

class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        ret = []
        for term in terms:
            ret.append(datetime.utcnow() + parse_time_interval(term)).strftime('%FT%TZ')

        return ret
