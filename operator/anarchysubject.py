from datetime import datetime
import kubernetes
import logging
import os
import queue
import threading
import time
import uuid

cache_age_limit = int(os.environ.get('ANARCHY_SUBJECT_CACHE_AGE_LIMIT', 600))

from anarchygovernor import AnarchyGovernor

operator_logger = logging.getLogger('operator')

class AnarchySubjectRunQueue(object):
    queues = {}
    active_subjects = set()

    @staticmethod
    def get(name, runtime):
        queue = AnarchySubjectRunQueue.queues.get(name, None)
        if queue:
            return queue.queue_get(runtime)

    @staticmethod
    def put(name, anarchy_subject_name, runtime):
        if name not in AnarchySubjectRunQueue.active_subjects:
            operator_logger.debug('Putting %s in %s run queue', anarchy_subject_name, name)
            AnarchySubjectRunQueue.active_subjects.add(anarchy_subject_name)
            queue = AnarchySubjectRunQueue.queue(name)
            queue.queue_put(anarchy_subject_name)

    @staticmethod
    def queue(name):
        queue = AnarchySubjectRunQueue.queues.get(name, None)
        if not queue:
            queue = AnarchySubjectRunQueue(name)
        return queue

    @staticmethod
    def release(anarchy_subject_name):
        if anarchy_subject_name in AnarchySubjectRunQueue.active_subjects:
            operator_logger.debug('Release AnarchySubject %s to re-enter run queue', anarchy_subject_name)
            AnarchySubjectRunQueue.active_subjects.remove(anarchy_subject_name)
        else:
            operator_logger.warning('Unable to release AnarchySubject %s, not in active_subjects', anarchy_subject_name)

    def __init__(self, name):
        self.name = name
        self.queue = queue.Queue()
        AnarchySubjectRunQueue.queues[name] = self

    def queue_get(self, runtime):
        while True:
            try:
                name = self.queue.get_nowait()
                operator_logger.debug('Got AnarchySubject %s from run queue %s', name, self.name)
                anarchy_subject = AnarchySubject.get(name, runtime)
                if anarchy_subject:
                    return anarchy_subject
            except queue.Empty:
                return

    def queue_put(self, anarchy_subject_name):
        self.queue.put(anarchy_subject_name)

class AnarchySubject(object):
    """AnarchySubject class"""

    # Cache of active AnarchySubjects
    cache = {}
    cache_lock = threading.Lock()

    @staticmethod
    def cache_clean():
        try:
            AnarchySubject.cache_lock.acquire()
            for anarchy_subject_name in list(AnarchySubject.cache.keys()):
                anarchy_subject = AnarchySubject.cache[anarchy_subject_name]
                if not anarchy_subject.current_anarchy_run \
                and time.time() - anarchy_subject.last_active > cache_age_limit:
                    del AnarchySubject.cache[anarchy_subject_name]
        finally:
            AnarchySubject.cache_lock.release()

    @staticmethod
    def cache_put(anarchy_subject):
        anarchy_subject.last_active = time.time()
        AnarchySubject.cache[anarchy_subject.name] = anarchy_subject

    @staticmethod
    def cache_update(resource):
        """Update subject in cache if present in cache"""
        resource_meta = resource['metadata']
        anarchy_subject_name = resource_meta['name']
        anarchy_subject = AnarchySubject.cache.get(anarchy_subject_name, None)
        if anarchy_subject:
            anarchy_subject.metadata = resource_meta
            anarchy_subject.spec = resource['spec']
            anarchy_subject.status = resource.get('status', None)
            return anarchy_subject

    @staticmethod
    def get(name, runtime):
        """Get subject by name from cache or get resource"""
        try:
            AnarchySubject.cache_lock.acquire()
            subject = AnarchySubject.cache.get(name, None)
            if subject:
                operator_logger.debug('Got AnarchySubject %s from cache', name)
                subject.last_active = time.time()
                return subject
            try:
                operator_logger.debug('Getting AnarchySubject %s', name)
                resource = runtime.custom_objects_api.get_namespaced_custom_object(
                    runtime.operator_domain, 'v1', runtime.operator_namespace,
                    'anarchysubjects', name
                )
            except kubernetes.client.rest.ApiException as e:
                if e.status == 404:
                    return None
                else:
                    raise
            subject = AnarchySubject(resource)
            AnarchySubject.cache_put(subject)
            return subject
        finally:
            AnarchySubject.cache_lock.release()

    @staticmethod
    def get_from_resource(resource):
        """Get subject by name from cache or get resource"""
        try:
            AnarchySubject.cache_lock.acquire()
            subject = AnarchySubject.cache_update(resource)
            if not subject:
                subject = AnarchySubject(resource)
                AnarchySubject.cache_put(subject)
            return subject
        finally:
            AnarchySubject.cache_lock.release()

    @staticmethod
    def get_pending(runner_queue_name, runtime):
        """Get AnarchySubject with pending ansible runs or return None"""
        return AnarchySubjectRunQueue.get(runner_queue_name, runtime)

    @staticmethod
    def retry_failures(runtime):
        for anarchy_subject in AnarchySubject.cache.values():
            if anarchy_subject.current_anarchy_run \
            and anarchy_subject.retry_after \
            and anarchy_subject.retry_after < datetime.utcnow():
                operator_logger.warning(
                    'Retrying AnarchyRun %s', anarchy_subject.current_anarchy_run
                )
                anarchy_subject.retry_after = None
                anarchy_subject.put_in_job_queue(runtime)

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        # Last activity on subject, used to manage caching
        self.last_active = 0
        self.retry_after = None
        self.anarchy_run_lock = threading.Lock()
        self.anarchy_run_queue = queue.Queue()
        self.anarchy_runs = {}
        self.current_anarchy_run = None
        self.__sanity_check()

    def __sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    @property
    def delete_started(self):
        return self.status and 'deleteHandlersStarted' in self.status

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

    @property
    def kind(self):
        return 'AnarchySubject'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def namespace(self):
        return self.metadata['namespace']

    @property
    def parameters(self):
        return self.spec.get('parameters', {})

    @property
    def parameter_secrets(self):
        return self.spec.get('parameterSecrets', [])

    @property
    def resource_version(self):
        return self.metadata['resourceVersion']

    @property
    def uid(self):
        return self.metadata['uid']

    @property
    def vars(self):
        return self.spec.get('vars', {})

    @property
    def var_secrets(self):
        return self.spec.get('varSecrets', [])

    def to_dict(self, runtime):
        return dict(
            apiVersion = runtime.operator_domain + '/v1',
            kind = 'AnarchySubject',
            metadata=self.metadata,
            spec=self.spec,
            status=self.status
        )

    def add_finalizer(self, runtime):
        finalizers = self.metadata.get('finalizers', [])
        if runtime.operator_domain not in finalizers:
            runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
                {'metadata': {'finalizers': finalizers + [runtime.operator_domain] } }
            )

    def enqueue_anarchy_run(self, anarchy_run, runtime):
        """Add anarchy_run to queue or update anarchy_run definition in queue if already present"""
        self.last_active = time.time()
        self.anarchy_run_lock.acquire()
        try:
            if anarchy_run.name in self.anarchy_runs:
                self.anarchy_runs[anarchy_run.name] = anarchy_run
                if self.current_anarchy_run == anarchy_run.name:
                    self.put_in_job_queue(runtime)
            else:
                self.anarchy_runs[anarchy_run.name] = anarchy_run
                if self.current_anarchy_run:
                    self.anarchy_run_queue.put(anarchy_run.name)
                else:
                    self.current_anarchy_run = anarchy_run.name
                    self.put_in_job_queue(runtime)
        finally:
            self.anarchy_run_lock.release()

    def anarchy_run_update(self, anarchy_run, runtime):
        self.last_active = time.time()
        self.anarchy_run_lock.acquire()
        try:
            self.anarchy_runs[anarchy_run.name] = anarchy_run
        finally:
            self.anarchy_run_lock.release()

    def delete(self, remove_finalizers, runtime):
        result = runtime.custom_objects_api.delete_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name
        )
        if remove_finalizers:
            self.remove_finalizers(runtime)
        return result

    def get_governor(self, runtime):
        governor = AnarchyGovernor.get(self.spec['governor'])
        if not governor:
            operator_logger.error('Unable to find governor %s', self.governor_name)
        return governor

    def get_anarchy_run(self, runtime):
        return self.anarchy_runs.get(self.current_anarchy_run, None)

    def handle_create(self, runtime):
        self.add_finalizer(runtime)
        self.process_subject_event_handlers(runtime, 'create')

    def handle_delete(self, runtime):
        '''
        Handle delete if delete process has not started. If there is a delete
        subject event handler then an AnarchyRun will be created to process the
        delete, otherwise the finalizers are removed immediately.
        '''
        if self.delete_started:
            return
        self.record_delete_started(runtime)
        event_handled = self.process_subject_event_handlers(runtime, 'delete')
        if not event_handled:
            self.remove_finalizers(runtime)

    def handle_spec_update(self, runtime):
        '''
        Handle update to AnarchySubject spec.
        '''
        self.process_subject_event_handlers(runtime, 'update')

    def patch(self, patch, runtime):
        '''
        Patch AnarchySubject resource and status.
        '''
        resource_patch = {}
        result = None
        if 'metadata' in patch:
            resource_patch['metadata'] = patch['metadata']
        if 'spec' in patch:
            resource_patch['spec'] = patch['spec']
        if resource_patch:
            result = runtime.custom_objects_api.patch_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchysubjects', self.name, resource_patch
            )
        if 'status' in patch:
            result = runtime.custom_objects_api.patch_namespaced_custom_object_status(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchysubjects', self.name, {'status': patch['status']}
            )
        return result

    def put_in_job_queue(self, runtime):
        operator_logger.info('Putting AnarchySubject %s in run queue', self.name)
        # FIXME - Allow for other job queues
        AnarchySubjectRunQueue.put('default', self.name, runtime)

    def process_subject_event_handlers(self, runtime, event_name):
        governor = self.get_governor(runtime)
        if not governor:
            operator_logger.warning(
                'Received "%s" event for subject "%s", but cannot find AnarchyGovernor %s',
                event_name, self.name, self.governor_name
            )
            return

        handler = governor.subject_event_handlers.get(event_name, None)
        if not handler:
            return

        context = (
            ('governor', governor),
            ('subject', self),
            ('handler', handler)
        )
        run_vars = {
            'anarchy_event_name': event_name
        }

        governor.run_ansible(runtime, handler, run_vars, context, self, None, event_name)
        return True

    def record_delete_started(self, runtime):
        runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def remove_finalizers(self, runtime):
        return runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': None } }
        )

    def remove_anarchy_run(self, anarchy_run, runtime):
        anarchy_run_name = anarchy_run.name
        self.anarchy_run_lock.acquire()
        try:
            try:
                del self.anarchy_runs[anarchy_run_name]
                operator_logger.info('Removed AnarchyRun %s from anarchy_runs', anarchy_run_name)
            except KeyError:
                operator_logger.debug('Removed AnarchyRun %s was not found in anarchy_runs', anarchy_run_name)

            if self.current_anarchy_run == anarchy_run_name:
                operator_logger.info('Removing current AnarchyRun %s for AnarchySubject %s', anarchy_run_name, self.name)
                self.current_anarchy_run = None
                while True:
                    try:
                        next_run_name = self.anarchy_run_queue.get_nowait()
                        if next_run_name in self.anarchy_runs:
                            operator_logger.debug(
                                'New current AnarchyRun is %s for AnarchySubject %s',
                                next_run_name, self.name
                            )
                            self.current_anarchy_run = next_run_name
                            self.put_in_job_queue(runtime)
                            break
                        else:
                            operator_logger.warning(
                                'AnarchyRun %s for AnarchySubject %s removed before processing',
                                next_run_name, self.name
                            )
                    except queue.Empty:
                        break
        finally:
            self.anarchy_run_lock.release()

    def run_queue_release(self):
        AnarchySubjectRunQueue.release(self.name)
