# (c) 2021, Johnathan Kupferer <jkupfere@redhat.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from base64 import b64decode, b64encode
import hashlib
import json

def anarchy_spec_sha256(spec):
    return b64encode(hashlib.sha256(json.dumps(
        spec, sort_keys=True, separators=(',',':')
    ).encode('utf-8')).digest()).decode('utf-8')

class FilterModule(object):
    def filters(self):
        return {
            'anarchy_spec_sha256': anarchy_spec_sha256
        }
