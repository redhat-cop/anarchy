from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible.inventory.host import Host
from ansible.plugins.callback.default import CallbackModule as CallbackModule_default

import datetime
import json
import os

DOCUMENTATION = '''
callback: anarchy
type: stdout
short_description: Anarchy stdout plugin
version_added: n/a
description:
- This is the output plugin for Anarchy
extends_documentation_fragment:
- default_callback
requirements:
- set as stdout in configuration
options:
  anarchy_output_dir:
    name: Directory in which to write output files.
    description:
    - "The anarchy output callback writes YAML to record details from the play"
    type: string
    default: playbook-result.yaml
    version_added: n/a
    env:
    - name: ANSIBLE_ANARCHY_OUTPUT_DIR
    ini:
    - key: anarchy_output_dir
      section: defaults
'''

def current_time():
    return '%sZ' % datetime.datetime.utcnow().isoformat()

def munge_result(result):
    """Return cleaned up and pruned version of result dict"""
    ret = result._result.copy()
    # Discard stdout_lines if result has stdout
    if 'stdout' in ret:
        ret.pop('stdout_lines', None)
    # Discard stderr_lines if result has stderr
    if 'stderr' in ret:
        ret.pop('stderr_lines', None)
    return ret

class CallbackModule(CallbackModule_default):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stdout'
    CALLBACK_NAME = 'anarchy'

    def __init__(self, display=None):
        super().__init__()
        self.anarchy_result_fh = None
        self.anarchy_task_hosts = None

    def anarchy_close_result_file(self):
        self.anarchy_result_fh.close()
        self.anarchy_result_fh = None

    def anarchy_open_result_file(self):
        result_file_path = os.path.join(self.get_option('anarchy_output_dir'), 'anarchy-result.yaml')
        if not self.anarchy_result_fh:
            self.anarchy_result_fh = open(result_file_path, 'w')
            self.anarchy_result_fh.write("---\nplays:\n")

    def anarchy_record_play_start(self, play):
        self.anarchy_open_result_file()
        self.anarchy_result_fh.write((
            '- name: {}\n'
            '  id: {}\n'
            '  start: {}\n'
            '  tasks:\n'
        ).format(
            json.dumps(play.get_name()),
            json.dumps(play._uuid),
            json.dumps(current_time())
        ))

    def anarchy_record_run_start(self, host, task):
        if host.name not in self.anarchy_task_hosts:
            self.anarchy_task_hosts[host.name] = {}
        if task.args:
            self.anarchy_task_hosts[host.name]['args'] = task.args

    def anarchy_record_run_result(self, result, extra):
        host = result._host.name
        if host not in self.anarchy_task_hosts:
            self.anarchy_task_hosts[host] = {}
        self.anarchy_task_hosts[host]['result'] = munge_result(result)
        self.anarchy_task_hosts[host].update(extra)

    def anarchy_record_item(self, result, extra):
        host = result._host.name
        if host not in self.anarchy_task_hosts:
            items = []
            self.anarchy_task_hosts[host] = {'items': items}
        elif 'items' not in self.anarchy_task_hosts[host]:
            items = []
            self.anarchy_task_hosts[host]['items'] = items
        else:
            items = self.anarchy_task_hosts[host]['items']
        item = extra.copy()
        item['item'] = self._get_item_label(result._result)
        item['result'] = munge_result(result)
        items.append(item)

    def anarchy_record_stats(self, stats):
        self.anarchy_result_fh.write('  stats:\n')
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            self.anarchy_result_fh.write('    {}:\n'.format(h))
            s = stats.summarize(h)
            for k, v in s.items():
                self.anarchy_result_fh.write('      {}: {}\n'.format(k, json.dumps(v)))

    def anarchy_record_task_end(self):
        self.anarchy_result_fh.write((
            '    end: {}\n'
            '    hosts:\n'
        ).format(json.dumps(current_time())))

        for host, host_data in self.anarchy_task_hosts.items():
            self.anarchy_result_fh.write('      {}:\n'.format(host))
            for k, v in host_data.items():
                if k not in ('items', 'args', 'result'):
                    self.anarchy_result_fh.write('        {}: {}\n'.format(
                        k, json.dumps(v)
                    ))

            if 'args' in host_data:
                self.anarchy_result_fh.write('        args:\n')
                for k, v in host_data['args'].items():
                    self.anarchy_result_fh.write('          {}: {}\n'.format(
                        k, json.dumps(v)
                    ))

            if 'result' in host_data:
                self.anarchy_result_fh.write('        result:\n')
                for k, v in host_data['result'].items():
                    self.anarchy_result_fh.write('          {}: {}\n'.format(
                        k, json.dumps(v)
                    ))

            if 'items' in host_data:
                self.anarchy_result_fh.write('        items:\n')
                for item in host_data['items']:
                    first = True
                    self.anarchy_result_fh.write('        - item: {}\n'.format(
                        json.dumps(item['item'])
                    ))
                    for k, v in item.items():
                        if k not in ('item', 'result'):
                            self.anarchy_result_fh.write('          {}: {}\n'.format(
                                k, json.dumps(v)
                            ))
                    if 'args' in item:
                        self.anarchy_result_fh.write('          args:\n')
                        for k, v in item['args'].items():
                            if not k.startswith('_'):
                                self.anarchy_result_fh.write('            {}: {}\n'.format(
                                    k, json.dumps(v)
                                ))
                                first = False
                    if 'result' in item:
                        self.anarchy_result_fh.write('          result:\n')
                        for k, v in item['result'].items():
                            if not k.startswith('_'):
                                self.anarchy_result_fh.write('            {}: {}\n'.format(
                                    k, json.dumps(v)
                                ))
                                first = False


    def anarchy_record_task_start(self, task):
        self.anarchy_result_fh.write((
            '  - name: {}\n'
            '    action: {}\n'
            '    id: {}\n'
            '    start: {}\n'
        ).format(
            json.dumps(task.get_name()),
            json.dumps(task.action),
            json.dumps(task._uuid),
            json.dumps(current_time())
        ))
        self.anarchy_task_hosts = {}

    def v2_playbook_on_play_start(self, play):
        super().v2_playbook_on_play_start(play)
        self.anarchy_record_play_start(play)

    def v2_playbook_on_stats(self, stats):
        super().v2_playbook_on_stats(stats)
        if self.anarchy_task_hosts:
            self.anarchy_record_task_end()
        self.anarchy_record_stats(stats)
        self.anarchy_close_result_file()

    def v2_playbook_on_handler_task_start(self, task):
        super().v2_playbook_on_handler_task_start(task)
        if self.anarchy_task_hosts:
            self.anarchy_record_task_end()
        self.anarchy_record_task_start(task)

    def v2_playbook_on_task_start(self, task, is_conditional):
        super().v2_playbook_on_task_start(task, is_conditional)
        if self.anarchy_task_hosts:
            self.anarchy_record_task_end()
        self.anarchy_record_task_start(task)

    def v2_runner_on_start(self, host, task):
        super().v2_runner_on_start(host, task)
        self.anarchy_record_run_start(host, task)

    def v2_runner_item_on_failed(self, result):
        super().v2_runner_item_on_failed(result)
        self.anarchy_record_item(result, dict(failed=True))

    def v2_runner_item_on_ok(self, result):
        super().v2_runner_item_on_ok(result)
        self.anarchy_record_item(result, dict(ok=True))

    def v2_runner_item_on_skipped(self, result):
        super().v2_runner_item_on_ok(result)
        self.anarchy_record_item(result, dict(skipped=True))

    def v2_runner_on_failed(self, result, ignore_errors=False):
        super().v2_runner_on_failed(result, ignore_errors)
        self.anarchy_record_run_result(result, dict(failed=True))

    def v2_runner_on_ok(self, result):
        super().v2_runner_on_ok(result)
        self.anarchy_record_run_result(result, dict(ok=True))

    def v2_runner_on_skipped(self, result):
        super().v2_runner_on_skipped(result)
        self.anarchy_record_run_result(result, dict(skipped=True))
