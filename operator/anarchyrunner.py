import copy
import json
import kopf
import kubernetes
import logging
import os
import re
import threading
import time
import subprocess
import urllib3

from anarchyutil import deep_update, random_string

operator_logger = logging.getLogger('operator')

class AnarchyRunner:
    """Represents a pool of runner pods to process for AnarchyGovernors"""

    all_in_one_token = random_string(50)
    register_lock = threading.Lock()
    runners = {}

    @staticmethod
    def check_runner_auth(auth_header, anarchy_runtime):
        '''
        Verify bearer token sent by anarchy runner pod in API call and return
        the AnarchyRunner definition.
        '''
        match = re.match(r'Bearer ([^:]+):([^:]+):(.*)', auth_header)
        if not match:
            operator_logger.warning('Failed AnarchyRunner auth, no Authorization header!')
            return None, None, None, None

        runner_name = match.group(1)
        pod_name = match.group(2)
        auth_runner_token = match.group(3)

        pod_ref = dict(
            apiVersion = 'v1',
            kind = 'Pod',
            name = pod_name,
            namespace = anarchy_runtime.operator_namespace,
        )

        anarchy_runner = AnarchyRunner.get(runner_name)
        if not anarchy_runner:
            operator_logger.warning(
                'Failed AnarchyRunner auth, AnarchyRunner not found',
                extra = dict(
                    runner = dict(
                        apiVersion = anarchy_runtime.api_group_version,
                        kind = 'AnarchyRunner',
                        name = runner_name,
                        namespace = anarchy_runtime.operator_namespace
                    ),
                    pod = pod_ref,
                )
            )
            return None, None, None, None

        runner_pod = anarchy_runner.get_pod(pod_name)
        if not runner_pod:
            operator_logger.warning(
                'Failed AnarchyRunner auth, AnarchyRunner Pod not found',
                extra = dict(
                    runner = anarchy_runner.reference,
                    client = pod_ref,
                )
            )
            return None, None, None, None

        if not anarchy_runtime.running_all_in_one:
            pod_ref['uid'] = runner_pod.metadata.uid

        # We check the token against the pod environment variable rather than
        # the AnarchyRunner's definition to support token rotation. If an
        # attacker can create a pod in the operator namespace then they already
        # have full access anyway.
        runner_token = None
        for env_var in runner_pod.spec.containers[0].env:
            if env_var.name == 'RUNNER_TOKEN':
                runner_token = env_var.value
                break

        if not runner_token:
            operator_logger.warning(
                'Failed AnarchyRunner auth, cannot find RUNNER_TOKEN env variable',
                extra = dict(
                    runner = anarchy_runner.reference,
                    client = pod_ref,
                )
            )
            return None, None, None, None

        if auth_runner_token != runner_token:
            operator_logger.warning(
                'Failed AnarchyRunner auth, invalid authentication token',
                extra = dict(
                    runner = anarchy_runner.reference,
                    client = pod_ref,
                )
            )
            return None, None, None, None

        run_ref, subject_ref = anarchy_runner.get_current_run_and_subject(pod_ref)
        return anarchy_runner, runner_pod, run_ref, subject_ref

    @staticmethod
    def create_default(anarchy_runtime):
        AnarchyRunner.register(
            resource_object = anarchy_runtime.custom_objects_api.create_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyrunners',
                {
                    'apiVersion': anarchy_runtime.api_group_version,
                    'kind': 'AnarchyRunner',
                    'metadata': {
                        'name': 'default',
                        # Default AnarchyRunner is owned by the Anarchy Service to automate cleanup
                        'ownerReferences': [{
                            'apiVersion': anarchy_runtime.anarchy_service.api_version,
                            'controller': True,
                            'kind': anarchy_runtime.anarchy_service.kind,
                            'name': anarchy_runtime.anarchy_service.metadata.name,
                            'uid': anarchy_runtime.anarchy_service.metadata.uid
                        }]
                    },
                    'spec': {
                        'minReplicas': 1,
                        'maxReplicas': 9,
                        'token': random_string(50),
                        'podTemplate': {
                            'spec': {
                                'serviceAccountName': 'anarchy-runner-default',
                                'containers': [{
                                    'name': 'runner',
                                    'resources': {
                                        'limits': {
                                            'cpu': '1',
                                            'memory': '512Mi',
                                        },
                                        'requests': {
                                            'cpu': '500m',
                                            'memory': '512Mi',
                                        },
                                    },
                                }]
                            }
                        }
                    }
                }
            )
        )

    @staticmethod
    def get(name):
        return AnarchyRunner.runners.get(name, None)

    @staticmethod
    def get_resource_from_api(name, anarchy_runtime):
        try:
            return anarchy_runtime.custom_objects_api.get_namespaced_custom_object(
                anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                anarchy_runtime.operator_namespace, 'anarchyrunners', name
            )
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def preload(anarchy_runtime):
        '''
        Load all AnarchyRunners and pods for the AnarchyRunners.

        This method is used during start-up to ensure that all AnarchyRunner definitions are
        loaded before processing starts.
        '''
        operator_logger.info("Starting AnarchyRunner preload")
        for resource_object in anarchy_runtime.custom_objects_api.list_namespaced_custom_object(
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyrunners'
        ).get('items', []):
            AnarchyRunner.register(resource_object=resource_object)

        for pod in anarchy_runtime.core_v1_api.list_namespaced_pod(
            anarchy_runtime.operator_namespace, label_selector=anarchy_runtime.runner_label
        ).items:
            runner_name = pod.metadata.labels[anarchy_runtime.runner_label]
            runner = AnarchyRunner.get(runner_name)
            if runner:
                runner.register_pod(pod, anarchy_runtime)
            else:
                operator_logger.warning(
                    'Preload found runner pod without matching AnarchyRunner',
                    extra = dict(
                        pod = dict(
                            apiVersion = 'v1',
                            kind = 'Pod',
                            name = pod.metadata.name,
                            namespace = pod.metadata.namespace,
                            uid = pod.metadata.uid,
                        ),
                        runner = dict(
                            apiVersion = anarchy_runtime.api_group_version,
                            kind = 'AnarchyRunner',
                            name = runner_name,
                            namespace = anarchy_runtime.operator_namespace
                        )
                    )
                )

    @staticmethod
    def register(
        anarchy_runtime=None,
        annotations=None,
        labels=None,
        logger=None,
        meta=None,
        name=None,
        namespace=None,
        resource_object=None,
        spec=None,
        status=None,
        uid=None,
        **_
    ):
        '''
        Register AnarchyRunner or refresh definition of already registered AnarchyRunner
        '''
        with AnarchyRunner.register_lock:
            if resource_object:
                name = resource_object['metadata']['name']
            else:
                resource_object = dict(
                    apiVersion = anarchy_runtime.api_group_version,
                    kind = 'AnarchyRunner',
                    metadata = dict(
                        annotations = dict(annotations) if annotations else None,
                        creationTimestamp = meta["creationTimestamp"],
                        deletionTimestamp = meta.get("deletionTimestamp"),
                        labels = dict(labels) if labels else None,
                        name = name,
                        namespace = namespace,
                        resourceVersion = meta["resourceVersion"],
                        uid = uid,
                    ),
                    spec = dict(spec),
                    status = dict(status) if status else {},
                )

            runner = AnarchyRunner.runners.get(name)
            if runner:
                runner.__init__(logger=logger, resource_object=resource_object)
                runner.local_logger.debug("Refreshed AnarchyRunner")
            else:
                runner = AnarchyRunner(logger=logger, resource_object=resource_object)
                AnarchyRunner.runners[name] = runner
                runner.local_logger.info("Registered AnarchyRunner")
            return runner

    @staticmethod
    def start_all_in_one_runner(anarchy_runtime):
        operator_logger.info('Starting all-in-on runner')

        # Register default runner
        runner = AnarchyRunner.register(
            resource_object = {
                "apiVersion": anarchy_runtime.api_group_version,
                "kind": "AnarchyRunner",
                "metadata": {
                    "name": "default",
                    "namespace": anarchy_runtime.operator_namespace,
                    "uid": "-",
                },
                "spec": {
                    "token": AnarchyRunner.all_in_one_token,
                },
                "status": {
                    "pods": [{
                        "name": os.environ['HOSTNAME'],
                    }]
                }
            }
        )

        # Add dummy pod definition into runner
        runner.register_pod(
            kubernetes.client.V1Pod(
                api_version = 'v1',
                kind = 'Pod',
                metadata = kubernetes.client.V1ObjectMeta(
                    name = os.environ['HOSTNAME'],
                    namespace = anarchy_runtime.operator_namespace,
                    labels = {}
                ),
                spec = kubernetes.client.V1PodSpec(
                    containers = [
                        kubernetes.client.V1Container(
                            name = 'runner',
                            env = [
                                kubernetes.client.V1EnvVar(
                                    name = 'RUNNER_TOKEN',
                                    value = AnarchyRunner.all_in_one_token,
                                )
                            ]
                        )
                    ]
                )
            ),
            anarchy_runtime
        )

        env = os.environ.copy()
        env['ANARCHY_COMPONENT'] = 'runner'
        env['ANARCHY_URL'] = 'http://{}:5000'.format(anarchy_runtime.anarchy_service_name)
        env['RUNNER_NAME'] = 'default'
        env['RUNNER_TOKEN'] = AnarchyRunner.all_in_one_token
        subprocess.Popen(['/opt/app-root/src/.s2i/bin/run'], env=env)

    @staticmethod
    def unregister(name):
        '''
        Unregister AnarchyRunner
        '''
        with AnarchyRunner.register_lock:
            if name in AnarchyRunner.runners:
                runner = AnarchyRunner.runners.pop(name)
                runner.local_logger.info("Unregistered AnarchyRunner")
                return runner

    @staticmethod
    def watch(anarchy_runtime):
        '''
        Watch AnarchyRunners and keep definitions synchronized

        This watch is independent of the kopf watch and is used to keep runner definitions updated
        even when the pod is not the active peer.
        '''
        while True:
            try:
                AnarchyRunner.__watch(anarchy_runtime)
            except kubernetes.client.rest.ApiException as e:
                if e.status == 410:
                    # 410 Gone, simply reset watch
                    operator_logger.warning(
                        "Restarting AnarchyRunner watch",
                        extra = dict(reason = str(e))
                    )
                else:
                    operator_logger.exception("ApiException in AnarchyRunner watch")
                    time.sleep(5)
            except urllib3.exceptions.ProtocolError as e:
                operator_logger.warning(
                    "ProtocolError in AnarchyRunner watch",
                    extra = dict(reason = str(e))
                )
                time.sleep(5)
            except Exception as e:
                operator_logger.exception("Exception in AnarchyRunner watch")
                time.sleep(5)

    @staticmethod
    def __watch(anarchy_runtime):
        operator_logger.info("Starting AnarchyRunner watch")
        for event in kubernetes.watch.Watch().stream(
            anarchy_runtime.custom_objects_api.list_namespaced_custom_object,
            anarchy_runtime.operator_domain, anarchy_runtime.api_version, anarchy_runtime.operator_namespace, 'anarchyrunners'
        ):
            obj = event.get('object')
            if not obj:
                continue

            if event['type'] == 'ERROR' \
            and obj['kind'] == 'Status':
                if obj['status'] == 'Failure':
                    if obj['reason'] in ('Expired', 'Gone'):
                        operator_logger.info(
                            'AnarchyRunner watch restarting',
                            extra = dict(event = event)
                        )
                        return
                    else:
                        operator_logger.error(
                            "AnarchyRunner watch error",
                            extra = dict(event = event)
                        )
                        time.sleep(5)
                        return

            if obj.get('apiVersion') == anarchy_runtime.api_group_version:
                if event['type'] == 'DELETED':
                    AnarchyRunner.unregister(name=obj['metadata']['name'])
                else:
                    AnarchyRunner.register(resource_object=obj)

    @staticmethod
    def watch_pods(anarchy_runtime):
        '''
        Watch Pods with runner label and keep list up to date
        '''
        while True:
            try:
                AnarchyRunner.__watch_pods(anarchy_runtime)
            except kubernetes.client.rest.ApiException as e:
                if e.status == 410:
                    # 410 Gone, simply reset watch
                    operator_logger.warning(
                        "Restarting AnarchyRunner Pod watch",
                        extra = dict(reason = str(e))
                    )
                else:
                    operator_logger.exception("ApiException in AnarchyRunner Pod watch")
                    time.sleep(5)
            except urllib3.exceptions.ProtocolError as e:
                operator_logger.warning(
                    "ProtocolError in AnarchyRunner Pod watch",
                    extra = dict(reason = str(e))
                )
                time.sleep(5)
            except Exception as e:
                operator_logger.exception("Exception in AnarchyRunner Pod watch")
                time.sleep(5)

    @staticmethod
    def __watch_pods(anarchy_runtime):
        operator_logger.info("Starting AnarchyRunner Pod watch")
        for event in kubernetes.watch.Watch().stream(
            anarchy_runtime.core_v1_api.list_namespaced_pod,
            anarchy_runtime.operator_namespace, label_selector=anarchy_runtime.runner_label
        ):
            obj = event.get('object')
            if not obj:
                continue

            if event['type'] == 'ERROR' \
            and not isinstance(obj, kubernetes.client.V1Pod) \
            and obj['kind'] == 'Status':
                if obj['status'] == 'Failure':
                    if obj['reason'] in ('Expired', 'Gone'):
                        operator_logger.info(
                            'AnarchyRunner Pod watch restarting',
                            extra = dict(event = event)
                        )
                        return
                    else:
                        operator_logger.error(
                            "AnarchyRunner Pod watch error",
                            extra = dict(event = event)
                        )
                        time.sleep(5)
                        return

            if obj and isinstance(obj, kubernetes.client.V1Pod):
                runner_name = obj.metadata.labels[anarchy_runtime.runner_label]
                runner = AnarchyRunner.get(runner_name)
                if event['type'] == 'DELETED':
                    if runner:
                        runner.unregister_pod(obj.metadata.name, anarchy_runtime)
                else:
                    if runner:
                        runner.register_pod(obj, anarchy_runtime)
                    else:
                        operator_logger.warning(
                            'Watch found runner pod without matching AnarchyRunner',
                            extra = dict(
                                pod = dict(
                                    apiVersion = 'v1',
                                    kind = 'Pod',
                                    name = obj.metadata.name,
                                    namespace = obj.metadata.namespace,
                                    uid = obj.metadata.uid,
                                ),
                                runner = dict(
                                    apiVersion = anarchy_runtime.api_group_version,
                                    kind = 'AnarchyRunner',
                                    name = runner_name,
                                    namespace = anarchy_runtime.operator_namespace
                                )
                            )
                        )

    def __init__(self, resource_object, logger=None):
        prev_token = self.spec.get('token') if hasattr(self, 'spec') else None

        self.api_version = resource_object['apiVersion']
        self.kind = resource_object['kind']
        self.metadata = resource_object['metadata']
        self.spec = resource_object['spec']
        self.status = resource_object.get('status', {})

        self.local_logger = kopf.LocalObjectLogger(
            body = resource_object,
            settings = kopf.OperatorSettings(),
        )
        if logger:
            self.logger = logger
        elif not hasattr(self, 'logger'):
            self.logger = self.local_logger

        if not hasattr(self, 'lock'):
            self.lock = threading.RLock()

        if not hasattr(self, 'pods'):
            self.pods = {}

        if not 'pods' in self.status:
            self.status['pods'] = []

        if not self.spec.get('token'):
            self.spec['token'] = prev_token or random_string(50)

    @property
    def image_pull_policy(self):
        return self.spec.get('imagePullPolicy', os.environ.get('RUNNER_IMAGE_PULL_POLICY', 'Always'))

    @property
    def max_replicas(self):
        return self.spec.get('maxReplicas', self.min_replicas)

    @property
    def min_replicas(self):
        return self.spec.get('minReplicas', 1)

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def owner_reference(self):
        return dict(
            apiVersion = self.api_version,
            controller = True,
            kind = self.kind,
            name = self.name,
            uid = self.uid,
        )

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def pod_namespace(self):
        return self.spec.get('podTemplate', {}).get('metadata', {}).get('namespace', None)

    @property
    def pod_template(self):
        return self.spec.get('podTemplate', {})

    @property
    def reference(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            name = self.name,
            namespace = self.namespace,
            uid = self.uid,
        )

    @property
    def resources(self):
        return self.spec.get('resources', {
            'limits': { 'cpu': '1', 'memory': '256Mi' },
            'requests': { 'cpu': '200m', 'memory': '256Mi' },
        })

    @property
    def runner_token(self):
        '''
        Return runner token, used to authenticate callbacks.
        Default to use object uid if token is not set.
        '''
        return self.spec.get('token', self.uid)

    @property
    def uid(self):
        return self.metadata.get('uid')

    def clear_pod_run_reference(self, anarchy_runtime, runner_pod_name, run_name=None):
        runner_pod_ref = dict(
            apiVersion = 'v1',
            kind = 'Pod',
            name = runner_pod_name,
            namespace = self.namespace,
        )
        with self.lock:
            while True:
                runner_pod_found = False
                run_ref = None
                resource_object = self.to_dict()
                subject_ref = None

                for status_pod in resource_object['status']['pods']:
                    if status_pod['name'] == runner_pod_name:
                        runner_pod_found = True
                        run_ref = status_pod.pop('run', None)
                        subject_ref = status_pod.pop('subject', None)
                        if run_name \
                        and (not run_ref or run_ref['name'] != run_name):
                            return

                if not runner_pod_found:
                    self.local_logger.warning(
                        'Did not find pod in AnarchyRunner status when clearing AnarchyRun!',
                        extra = dict(
                            pod = runner_pod_ref,
                        )
                    )
                    return

                if not run_ref:
                    # Indicates runner pod was found but run was already cleared
                    return

                if anarchy_runtime.running_all_in_one:
                    self.__init__(resource_object)
                    return

                try:
                    resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                        anarchy_runtime.operator_namespace, 'anarchyrunners', self.name,
                        resource_object
                    )
                    self.__init__(resource_object)
                    self.local_logger.info(
                        'Cleared AnarchyRun for AnarchyRunner Pod',
                        extra = dict(
                            pod = runner_pod_ref,
                            run = run_ref,
                            subject = subject_ref,
                        )
                    )
                    return
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 409:
                        # Conflict, refresh from api and retry
                        if not self.refresh_from_api(anarchy_runtime):
                            self.logger.error(
                                'Failed to refresh AnarchyRunner to clear AnarchyRun from status!',
                                extra = dict(
                                    pod = runner_pod_ref,
                                    run = run_ref,
                                    subject = subject_ref,
                                )
                            )
                            return
                    else:
                        raise

    def get_current_run_and_subject(self, pod_ref):
        pod_name = pod_ref.get('name')
        for status_pod in self.status.get('pods', []):
            if status_pod['name'] == pod_name:
                return status_pod.get('run'), status_pod.get('subject')
        self.local_logger.warning(
            "Attempt to get current run for Pod not listed in AnarchyRunner status!",
            extra = dict(pod = pod_ref)
        )
        return None, None

    def get_pod(self, name):
        return self.pods.get(name)

    def manage(self, anarchy_runtime):
        '''
        Manage Pods for AnarchyRunner
        '''
        with self.lock:
            # Make sure the runner service account exists
            self.manage_runner_service_account(anarchy_runtime)
            self.manage_runner_pods(anarchy_runtime)

    def manage_runner_pods(self, anarchy_runtime):
        '''
        Manage Pods for AnarchyRunner
        '''

        #deployment_name = 'anarchy-runner-' + self.name
        #deployment_namespace = self.pod_namespace or anarchy_runtime.operator_namespace

        pod_template = copy.deepcopy(self.pod_template)
        if 'metadata' not in pod_template:
            pod_template['metadata'] = {}
        if 'labels' not in pod_template['metadata']:
            pod_template['metadata']['labels'] = {}
        if 'spec' not in pod_template:
            pod_template['spec'] = {}
        if 'serviceAccountName' not in pod_template['spec']:
            pod_template['spec']['serviceAccountName'] = self.service_account_name(anarchy_runtime)
        if not 'containers' in pod_template['spec']:
            pod_template['spec']['containers'] = [{}]
        pod_template['metadata']['generateName'] = '{}-runner-{}-'.format(anarchy_runtime.anarchy_service_name, self.name)
        pod_template['metadata']['labels'][anarchy_runtime.runner_label] = self.name
        pod_template['metadata']['ownerReferences'] = [self.owner_reference]

        runner_container = pod_template['spec']['containers'][0]
        if 'name' not in runner_container:
            runner_container['name'] = 'runner'
        if not runner_container.get('image'):
            image = os.environ.get('RUNNER_IMAGE', '')
            if image != '':
                runner_container['image'] = image
            else:
                runner_container['image'] = anarchy_runtime.pod.spec.containers[0].image
        if not 'env' in runner_container:
            runner_container['env'] = []
        runner_container['env'].extend([
            {
                'name': 'ANARCHY_COMPONENT',
                'value': 'runner'
            },{
                'name': 'ANARCHY_URL',
                'value': 'http://{}.{}.svc:5000'.format(
                    anarchy_runtime.anarchy_service_name, anarchy_runtime.operator_namespace
                )
            },{
                'name': 'ANARCHY_DOMAIN',
                'value': anarchy_runtime.operator_domain
            },{
                'name': 'POD_NAME',
                'valueFrom': {
                    'fieldRef': {
                        'apiVersion': 'v1',
                        'fieldPath': 'metadata.name'
                    }
                }
            },{
                'name': 'RUNNER_NAME',
                'value': self.name
            },{
                'name': 'RUNNER_TOKEN',
                'value': self.runner_token
            }
        ])

        pod_count = 0
        for name, pod in self.pods.items():
            pod_dict = anarchy_runtime.api_client.sanitize_for_serialization(pod)
            if pod.metadata.labels.get(anarchy_runtime.runner_terminating_label) == 'true':
                # Ignore pod marked for termination
                pass
            elif pod_dict == deep_update(copy.deepcopy(pod_dict), pod_template):
                pod_count += 1
            else:
                # Pod does not match template, need to terminate pod
                pod = anarchy_runtime.core_v1_api.patch_namespaced_pod(
                    pod.metadata.name, pod.metadata.namespace,
                    { 'metadata': { 'labels': { anarchy_runtime.runner_terminating_label: 'true' } } }
                )
                self.logger.info(
                    'Labeled AnarchyRunner pod for termination',
                    extra = dict(
                        pod = dict(
                            apiVersion = 'v1',
                            kind = 'Pod',
                            name = pod.metadata.name,
                            namespace = pod.metadata.namespace,
                            uid = pod.metadata.uid,
                        )
                    )
                )

        # Add runner pods as needed to get to minimum count
        for i in range(self.min_replicas - pod_count):
            pod = None
            while pod == None:
                try:
                    pod = anarchy_runtime.core_v1_api.create_namespaced_pod(anarchy_runtime.operator_namespace, pod_template)
                    break
                except kubernetes.client.rest.ApiException as e:
                    if 'retry after the token is automatically created' in json.loads(e.body).get('message', ''):
                        time.sleep(1)
                    else:
                        raise
            self.register_pod(pod, anarchy_runtime)
            self.logger.info(
                'Started runner pod',
                extra = dict(
                    pod = dict(
                        apiVersion = pod.api_version,
                        kind = pod.kind,
                        name = pod.metadata.name,
                        namespace = pod.metadata.namespace,
                        uid = pod.metadata.uid,
                    )
                )
            )

    def manage_runner_service_account(self, anarchy_runtime):
        '''
        Create service account if not found
        '''
        name = self.service_account_name(anarchy_runtime)
        namespace = self.pod_namespace or anarchy_runtime.operator_namespace
        try:
            anarchy_runtime.core_v1_api.read_namespaced_service_account(name, namespace)
            return
        except kubernetes.client.rest.ApiException as e:
            if e.status != 404:
                raise
        service_account = anarchy_runtime.core_v1_api.create_namespaced_service_account(
            namespace,
            kubernetes.client.V1ServiceAccount(
                metadata = kubernetes.client.V1ObjectMeta(
                    name = name,
                    owner_references = [self.owner_reference],
                )
            )
        )
        self.logger.info(
            'Created AnarchyRunner ServiceAccount',
            extra = dict(
                serviceAccount = dict(
                    apiVersion = service_account.api_version,
                    kind = service_account.kind,
                    name = service_account.metadata.name,
                    namespace = service_account.metadata.namespace,
                    uid = service_account.metadata.uid,
                )
            )
        )

    def refresh_from_api(self, anarchy_runtime):
        resource_object = AnarchyRunner.get_resource_from_api(self.name, anarchy_runtime)
        if resource_object:
            self.__init__(resource_object)
            return True
        else:
            return False

    def register_pod(self, pod, anarchy_runtime):
        self.pods[pod.metadata.name] = pod
        with self.lock:
            while True:
                pod_is_new = True
                removed_pods = []
                status_pods = []
                resource_object = self.to_dict()

                for status_pod in self.status['pods']:
                    if status_pod['name'] in self.pods:
                        status_pods.append(status_pod)
                    else:
                        removed_pods.append(
                            dict(
                                apiVersion = 'v1',
                                kind = 'Pod',
                                name = status_pod['name'],
                                namespace = self.namespace
                            )
                        )
                    if pod.metadata.name == status_pod['name']:
                        pod_is_new = False

                if pod_is_new:
                    status_pods.append(dict(name=pod.metadata.name))

                if status_pods == self.status['pods']:
                    return

                try:
                    resource_object = self.to_dict()
                    resource_object['status']['pods'] = status_pods
                    resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                        anarchy_runtime.operator_namespace, 'anarchyrunners', self.name,
                        resource_object
                    )
                    self.__init__(resource_object)
                    self.local_logger.info(
                        'Registered AnarchyRunner Pod',
                        extra = dict(
                            added = dict(
                                apiVersion = pod.api_version,
                                kind = pod.kind,
                                name = pod.metadata.name,
                                namespace = pod.metadata.namespace,
                                uid = pod.metadata.uid,
                            ),
                            removed = removed_pods
                        )
                    )
                    return
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 409:
                        # Conflict, refresh from api and retry
                        if not self.refresh_from_api(anarchy_runtime):
                            self.logger.error(
                                'Failed to refresh AnarchyRunner register Pod in status!',
                                extra = dict(
                                    pod = runner_pod_reference,
                                    run = anarchy_run.reference,
                                )
                            )
                            return
                    else:
                        raise
        return pod

    def service_account_name(self, anarchy_runtime):
        return self.spec.get(
            'podTemplate', {}
        ).get(
            'spec', {}
        ).get(
            'serviceAccountName', anarchy_runtime.anarchy_service_name + '-runner-' + self.name
        )

    def set_pod_run_reference(self, anarchy_runtime, run, runner_pod):
        runner_pod_reference = dict(
            apiVersion = runner_pod.api_version,
            kind = runner_pod.kind,
            name = runner_pod.metadata.name,
            namespace = runner_pod.metadata.namespace,
            uid = runner_pod.metadata.uid,
        )
        with self.lock:
            while True:
                runner_pod_found = False
                resource_object = self.to_dict()

                for status_pod in resource_object['status']['pods']:
                    if status_pod['name'] == runner_pod.metadata.name:
                        runner_pod_found = True
                        status_pod['run'] = run.reference
                        status_pod['subject'] = run.subject_reference
                if not runner_pod_found:
                    self.local_logger.warning(
                        'Did not find pod in AnarchyRunner status when assinging AnarchyRun!',
                        extra = dict(
                            pod = runner_pod_reference,
                            run = run.reference,
                            subject = run.subject_reference,
                        )
                    )
                    self.status['pods'].append({
                        "name": runner_pod.metadata.name,
                        "run": run.reference,
                        "subject": run.subject_reference,
                    })

                if anarchy_runtime.running_all_in_one:
                    self.__init__(resource_object)
                    return

                try:
                    resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                        anarchy_runtime.operator_namespace, 'anarchyrunners', self.name,
                        resource_object
                    )
                    self.__init__(resource_object)
                    self.local_logger.info(
                        'Assigned AnarchyRun to AnarchyRunner Pod',
                        extra = dict(
                            pod = runner_pod_reference,
                            run = run.reference,
                            subject = run.subject_reference,
                        )
                    )
                    return
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 409:
                        # Conflict, refresh from api and retry
                        if not self.refresh_from_api(anarchy_runtime):
                            self.logger.error(
                                'Failed to refresh AnarchyRunner to add run to status!',
                                extra = dict(
                                    pod = runner_pod_reference,
                                    run = run.reference,
                                    subject = run.subject_reference,
                                )
                            )
                            return
                    else:
                        raise

    def to_dict(self):
        return dict(
            apiVersion = self.api_version,
            kind = self.kind,
            metadata = copy.deepcopy(self.metadata),
            spec = copy.deepcopy(self.spec),
            status = copy.deepcopy(self.status),
        )

    def unregister_pod(self, name, anarchy_runtime):
        pod = self.pods.pop(name, None)
        with self.lock:
            while True:
                status_pods = []
                removed_pods = []

                for status_pod in self.status['pods']:
                    if status_pod['name'] in self.pods:
                        status_pods.append(status_pod)
                    else:
                        removed_pods.append(
                            dict(
                                apiVersion = 'v1',
                                kind = 'Pod',
                                name = status_pod['name'],
                                namespace = self.namespace
                            )
                        )

                if not removed_pods:
                    return

                try:
                    resource_object = self.to_dict()
                    resource_object['status']['pods'] = status_pods
                    resource_object = anarchy_runtime.custom_objects_api.replace_namespaced_custom_object_status(
                        anarchy_runtime.operator_domain, anarchy_runtime.api_version,
                        anarchy_runtime.operator_namespace, 'anarchyrunners', self.name,
                        resource_object
                    )
                    self.__init__(resource_object)
                    self.local_logger.info(
                        'Unregistered AnarchyRunner Pod',
                        extra = dict(
                            removed = removed_pods
                        )
                    )
                    return
                except kubernetes.client.rest.ApiException as e:
                    if e.status == 409:
                        # Conflict, refresh from api and retry
                        if not self.refresh_from_api(anarchy_runtime):
                            self.logger.error(
                                'Failed to refresh AnarchyRunner unregister Pod from status!',
                                extra = dict(
                                    pod = runner_pod_reference,
                                    run = anarchy_run.reference,
                                )
                            )
                            return
                    else:
                        raise
        return pod
