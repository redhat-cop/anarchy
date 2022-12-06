import ansible_runner
import hashlib
import json
import logging
import os
import requests
import shutil
import subprocess
import time
import yaml

from base64 import b64encode
from datetime import datetime, timezone

from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction
from anarchyrun import AnarchyRun

class AnarchyGetRunException(Exception):
    pass

class AnarchyRunException(Exception):
    def __init__(self, rc, status, status_message, ansible_run=None):
        super().__init__(status_message)
        self.ansible_run = ansible_run
        self.rc = rc
        self.status = status

class AnarchyRunSetupException(Exception):
    pass

class AnarchyRunner:
    try:
        anarchy_url = os.environ['ANARCHY_URL']
        kubeconfig = os.environ['KUBECONFIG']
        pod_name = os.environ['HOSTNAME']
        runner_name = os.environ['RUNNER_NAME']
        runner_token = os.environ['RUNNER_TOKEN']
    except KeyError as e:
        raise Exception(f"Environment variable {e} must be defined")

    ansible_private_dir = os.environ.get('RUNNER_DIR', '/opt/app-root/anarchy-runner/.ansible')
    domain = os.environ.get('ANARCHY_DOMAIN', 'anarchy.gpte.redhat.com')
    output_dir = os.environ.get('OUTPUT_DIR', '/opt/app-root/anarchy-runner/output')
    polling_interval = int(os.environ.get('POLLING_INTERVAL', 5))
    runner_dir = os.environ.get('RUNNER_DIR', '/opt/app-root/anarchy-runner/ansible-runner')

    ansible_collections_dir = f"{ansible_private_dir}/collections"
    ansible_roles_dir = f"{ansible_private_dir}/roles"
    auth_header = f"Bearer {runner_name}:{pod_name}:{runner_token}"
    anarchy_run_data_path = f"{output_dir}/anarchy-run-data.yaml"
    anarchy_result_path = f"{output_dir}/anarchy-result.yaml"
    inventory_path = f"{runner_dir}/inventory"
    playbook_path = f"{runner_dir}/project/main.yml"

    if os.path.exists('/run/secrets/kubernetes.io/serviceaccount/namespace'):
        with open('/run/secrets/kubernetes.io/serviceaccount/namespace') as f:
            namespace = f.read()
    else:
        namespace = os.environ.get('ANARCHY_NAMESPACE')

    def get_run(self):
        try:
            response = requests.get(
                f"{self.anarchy_url}/run",
                headers = dict(Authorization=self.auth_header)
            )
            if response.status_code != 200:
                raise AnarchyGetRunException(f"{response.status_code} {response.text}")
            return response.json()
        except requests.exceptions.ConnectionError as e:
            raise AnarchyGetRunException(f"{e}")

    def post_result(self, anarchy_run, result, retries=10):
        for i in range(retries):
            try:
                response = requests.post(
                    f"{self.anarchy_url}/run/{anarchy_run.name}",
                    headers = dict(Authorization=self.auth_header),
                    json=dict(result=result),
                )
                if response.status_code != 200:
                    logging.warning('Failed to post run with status %s', response.status_code)
                return response
            except Exception as e:
                logging.exception("Exception when posting run")
                return
            time.sleep(self.polling_interval)

    def run(self, run_data):
        handler_definition = run_data['handler']
        handler_type = handler_definition['type']
        handler_name = handler_definition.get('name')
        handler_vars = handler_definition.get('vars', {})

        anarchy_governor = AnarchyGovernor(run_data['governor'])
        anarchy_subject = AnarchySubject(run_data['subject'])
        anarchy_action = AnarchyAction(run_data['action']) if handler_type in ('action', 'actionCallback') else None
        anarchy_run = AnarchyRun(run_data['run'])

        try:
            virtual_env = self.setup_run(
                anarchy_action = anarchy_action,
                anarchy_governor = anarchy_governor,
                anarchy_subject = anarchy_subject,
                anarchy_run = anarchy_run,
                handler_type = handler_type,
                handler_name = handler_name,
                handler_vars = handler_vars,
            )
        except AnarchyRunSetupException as e:
            logging.error(f"Failed to setup run: {e}")
            self.post_result(anarchy_run, dict(
                rc = 1,
                status = 'failed',
                statusMessage = f"Failed to setup run: {e}"
            ))
            return

        try:
            result = self.run_ansible(virtual_env)
        except AnarchyRunException as e:
            logging.error(f"{e}")
            result = dict(
                rc = e.rc,
                status = e.status,
                statusMessage = f"{e}"
            )
            if e.ansible_run:
                result['ansibleRun'] = e.ansible_run
            self.post_result(anarchy_run, result)
            return
        except Exception as e:
            logging.exception("Unhandled exception when running ansible")
            result = dict(
                 rc = 1,
                 status = 'failed',
                 statusMessage = f"Unhandled exception: {e}",
            )
            self.post_result(anarchy_run, result)
            return

        self.post_result(anarchy_run, result)

    def run_ansible(self, virtual_env):
        ansible_run = ansible_runner.interface.init_runner(
            playbook = 'main.yml',
            private_data_dir = self.runner_dir
        )
        ansible_run.config.env['ANSIBLE_STDOUT_CALLBACK'] = 'anarchy'

        if virtual_env:
            ansible_run.config.env['PATH'] = '{}/bin:{}'.format(virtual_env, os.environ['PATH'])
            ansible_run.config.env['VIRTUAL_ENV'] = virtual_env
            ansible_run.config.command[0] = virtual_env + '/bin/ansible-playbook'

        ansible_run.run()

        try:
            with open(self.anarchy_run_data_path) as f:
                run_data = yaml.safe_load(f)
        except Exception as e:
            raise AnarchyRunException(
                rc = ansible_run.rc,
                status = ansible_run.status,
                status_message = f"Failure loading anarchy run data: {e}",
            )

        if ansible_run.status != 'successful':
            # Get 'msg' from last task of last play on localhost for failure message
            try:
                status_message = run_data['plays'][-1]['tasks'][-1]['hosts']['localhost'].get('result', {}).get('msg', '')
            except Exception as e:
                status_message = "Unable to determine failure from run data: {e}"
            raise AnarchyRunException(
                ansible_run = run_data,
                rc = ansible_run.rc,
                status = ansible_run.status,
                status_message = status_message,
            )

        result = dict(
            ansibleRun = run_data,
            rc = 0,
            status = 'successful',
        )

        if os.path.exists(self.anarchy_result_path):
            try:
                with open(self.anarchy_result_path) as f:
                    result.update(yaml.safe_load(f))
            except Exception:
                logging.exception(f"Failure reading anarchy continue: {e}")

        return result

    def run_command(self, command, retries=5, delay=1):
        attempt = 0
        while True:
            attempt += 1
            try:
                subprocess.check_output(command, stderr=subprocess.STDOUT)
                return
            except subprocess.CalledProcessError:
                if attempt <= retries:
                    time.sleep(delay)
                else:
                    raise

    def run_loop(self):
        while True:
            try:
                run_data = self.get_run()
                if run_data:
                    self.run(run_data)
                else:
                    time.sleep(self.polling_interval)
            except AnarchyGetRunException as e:
                logging.error(f"Failed to get run: {e}")
                time.sleep(30)

    def setup_inventory(self,
        anarchy_action,
        anarchy_governor,
        anarchy_subject,
        anarchy_run,
        handler_type,
        handler_name,
        handler_vars,
        run_config,
    ):
        all_vars = {
            **anarchy_governor.vars,
            **run_config.vars,
            **anarchy_subject.vars,
        }
        if anarchy_action:
            all_vars.update(anarchy_action.vars)
        if handler_vars:
            all_vars.update(handler_vars)
        all_vars.update(dict(
            anarchy_domain = self.domain,
            anarchy_governor = anarchy_governor.export_for_inventory(),
            anarchy_governor_name = anarchy_governor.name,
            anarchy_namespace = self.namespace,
            anarchy_operator_domain = self.domain,
            anarchy_output_dir = self.output_dir,
            anarchy_run = anarchy_run.export_for_inventory(),
            anarchy_run_pod_name = self.pod_name,
            anarchy_run_timestamp = datetime.now(timezone.utc).strftime('%FT%TZ'),
            anarchy_runner_name = self.runner_name,
            anarchy_runner_token = self.runner_token,
            anarchy_subject = anarchy_subject.export_for_inventory(),
            anarchy_subject_name = anarchy_subject.name,
            anarchy_url = self.anarchy_url,
        ))
        if anarchy_action:
            all_vars.update(dict(
                anarchy_action = anarchy_action.export_for_inventory(),
                anarchy_action_name = anarchy_action.name,
                anarchy_action_callback_name_parameter = run_config.callback_name_parameter,
                anarchy_action_callback_token = anarchy_action.callback_token,
                anarchy_action_callback_url = anarchy_action.callback_url,
                anarchy_action_config_name = anarchy_action.action,
            ))
        if handler_type == 'actionCallback':
            all_vars.update(dict(
                anarchy_action_callback_name = handler_name,
            ))
        elif handler_type == 'subjectEvent':
            all_vars.update(dict(
                anarchy_event_name = handler_name,
            ))

        all_vars_dir = os.path.join(self.inventory_path, 'group_vars/all')
        all_vars_file = os.path.join(all_vars_dir, 'anarchy.json')

        try:
            if not os.path.isdir(all_vars_dir):
                os.makedirs(all_vars_dir)
        except Exception as e:
            raise AnarchyRunSetupException(f"Failed to create all vars inventory directory")

        try:
            with open(all_vars_file, mode='w') as f:
                f.write(json.dumps(all_vars))
        except Exception as e:
            raise AnarchyRunSetupException(f"Failed to write all vars inventory file")

    def setup_ansible_galaxy_requirements(self, anarchy_governor):
        requirements = anarchy_governor.ansible_galaxy_requirements
        if not requirements:
            return

        requirements_md5 = hashlib.md5(json.dumps(
            requirements, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')).hexdigest()

        requirements_dir = os.path.join(self.ansible_private_dir, f"requirements-{requirements_md5}")
        requirements_file = os.path.join(requirements_dir, 'requirements.yaml')
        requirements_collections_dir = os.path.join(requirements_dir, 'collections')
        requirements_roles_dir = os.path.join(requirements_dir, 'roles')
        if not os.path.exists(requirements_dir):
            os.makedirs(requirements_dir)
            os.mkdir(requirements_collections_dir)
            os.mkdir(requirements_roles_dir)
            with open(requirements_file, 'w') as f:
                yaml.safe_dump(requirements, stream=f)
        if os.path.lexists(self.ansible_collections_dir):
            os.unlink(self.ansible_collections_dir)
        if os.path.lexists(self.ansible_roles_dir):
            os.unlink(self.ansible_roles_dir)
        os.symlink(requirements_collections_dir, self.ansible_collections_dir)
        os.symlink(requirements_roles_dir, self.ansible_roles_dir)

        if requirements and 'collections' in requirements:
            try:
                self.run_command(['ansible-galaxy', 'collection', 'install', '-r', requirements_file])
            except subprocess.CalledProcessError as e:
                shutil.rmtree(requirements_dir)
                raise AnarchyRunSetupException(f"Failed ansible-galaxy collection install: {e.stdout.decode('utf-8')}")

        if requirements and 'roles' in requirements:
            try:
                self.run_command(['ansible-galaxy', 'role', 'install', '-r', requirements_file])
            except subprocess.CalledProcessError as e:
                shutil.rmtree(requirements_dir)
                raise AnarchyRunSetupException(f"Failed ansible-galaxy role install: {e.stdout.decode('utf-8')}")

    def setup_output_dir(self):
        try:
            filelist = os.listdir(self.output_dir)
        except Exception as e:
            raise AnarchyRunSetupException(f"Failed to list files in output dir for cleanup: {e}")
        try:
            for item in filelist:
                path = os.path.join(self.output_dir, item)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
        except Exception as e:
            raise AnarchyRunSetupException(f"Failed to remove {item} from output dir: {e}")

    def setup_playbook(self, play_name, run_config):
        plays = [dict(
            name = play_name,
            hosts = 'localhost',
            connection = 'local',
            gather_facts = False,
            pre_tasks = run_config.pre_tasks,
            roles = run_config.roles,
            tasks = run_config.tasks,
            post_tasks = run_config.post_tasks,
        )]
        try:
            with open(self.playbook_path, mode='w') as f:
                f.write(json.dumps(plays))
        except Exception as e:
            raise AnarchyRunSetupException(f"Failed to write playbook: {e}")

    def setup_run(self,
        anarchy_action,
        anarchy_governor,
        anarchy_subject,
        anarchy_run,
        handler_type,
        handler_name,
        handler_vars,
    ):
        if handler_type == 'action':
            # FIXME - add indication for continuation in play name
            play_name = f"{anarchy_run} - {anarchy_subject} - {anarchy_action} {anarchy_action.action}"
            run_config = anarchy_governor.get_action_config(anarchy_action.action)
        elif handler_type == 'actionCallback':
            play_name = f"{anarchy_run} - {anarchy_subject} - {anarchy_action} callback {handler_name}"
            run_config = anarchy_governor.get_action_callback_handler(anarchy_action.action, handler_name)
        elif handler_type == 'subjectEvent':
            play_name = f"{anarchy_run} - {anarchy_subject} - {handler_name} event handler"
            run_config = anarchy_governor.get_subject_event_handler(handler_name)
        else:
            raise AnarchyRunSetupException(f"Unknown handler type: {handler_type}")

        virtual_env = self.setup_virtual_env(anarchy_governor)
        self.setup_ansible_galaxy_requirements(anarchy_governor)
        self.setup_output_dir()
        self.setup_inventory(
            anarchy_action = anarchy_action,
            anarchy_governor = anarchy_governor,
            anarchy_run = anarchy_run,
            anarchy_subject = anarchy_subject,
            handler_type = handler_type,
            handler_name = handler_name,
            handler_vars = handler_vars,
            run_config = run_config,
        )
        self.setup_playbook(play_name, run_config)
        return virtual_env

    def setup_runner(self):
        if not os.path.exists(self.kubeconfig) \
        or 0 == os.path.getsize(self.kubeconfig):
            self.write_kubeconfig()

    def setup_virtual_env(self, anarchy_governor):
        requirements = anarchy_governor.python_requirements
        if not requirements:
            return

        requirements_md5 = hashlib.md5(requirements.encode('utf-8')).hexdigest()
        virtual_env = os.path.join(self.ansible_private_dir, f"pythonvenv-{requirements_md5}")
        if not os.path.exists(virtual_env):
            # Copy virtualenv from /opt/app-root
            os.makedirs(virtual_env)
            shutil.copy('/opt/app-root/pyvenv.cfg', os.path.join(virtual_env, 'pyenv.cfg'))
            shutil.copytree('/opt/app-root/bin', os.path.join(virtual_env, 'bin'))
            shutil.copytree('/opt/app-root/include', os.path.join(virtual_env, 'include'))
            shutil.copytree('/opt/app-root/lib', os.path.join(virtual_env, 'lib'))
            os.symlink('lib', os.path.join(virtual_env, 'lib64'))

            requirements_file = os.path.join(virtual_env, 'requirements.txt')
            with open(requirements_file, 'w') as fh:
                fh.write(requirements)

            try:
                self.run_command([os.path.join(virtual_env, "bin/pip3"), "install", "-r", requirements_file])
            except Exception as e:
                shutil.rmtree(virtual_env)
                raise

        return virtual_env

    def write_kubeconfig(self):
        with open('/run/secrets/kubernetes.io/serviceaccount/token') as f:
            kube_auth_token = f.read().strip()
        with open('/run/secrets/kubernetes.io/serviceaccount/ca.crt') as f:
            kube_ca_cert = f.read()
        with open(self.kubeconfig, 'w') as f:
            f.write(json.dumps({
                'apiVersion': 'v1',
                'clusters': [{
                    'name': 'cluster',
                    'cluster': {
                        'certificate-authority-data': \
                            b64encode(kube_ca_cert.encode('ascii')).decode('ascii'),
                        'server': 'https://kubernetes.default.svc.cluster.local'
                    }
                }],
                'contexts': [{
                    'name': 'ansible',
                    'context': {
                        'cluster': 'cluster',
                        'namespace': self.namespace,
                        'user': 'ansible'
                    }
                }],
                'current-context': 'ansible',
                'users': [{
                    'name': 'ansible',
                    'user': {
                        'token': kube_auth_token
                    }
                }]
            }))
