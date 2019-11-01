from datetime import datetime
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

    def release(anarchy_subject_name):
        if anarchy_subject_name in AnarchySubjectRunQueue.active_subjects:
            operator_logger.debug('Release AnarchySubject %s to re-enter run queue', anarchy_subject_name)
            AnarchySubjectRunQueue.active_subjects.remove(anarchy_subject_name)
        else:
            operator_logger.warn('Unable to release AnarchySubject %s, not in active_subjects', anarchy_subject_name)

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

    @staticmethod
    def cache_put(anarchy_subject):
        anarchy_subject.last_active = time.time()
        AnarchySubject.cache[anarchy_subject.name] = anarchy_subject

    @staticmethod
    def cache_clean():
        for anarchy_subject_name in list(AnarchySubject.cache.keys()):
            anarchy_subject = AnarchySubject.cache[anarchy_subject_name]
            if not anarchy_subject.current_event \
            and time.time() - anarchy_subject.last_active > cache_age_limit:
                del AnarchySubject.cache[anarchy_subject_name]

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
        anarchy_subject = AnarchySubject.cache.get(name, None)
        if anarchy_subject:
            anarchy_subject.last_active = time.time()
            return anarchy_subject

        try:
            resource = runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchysubjects', name
            )
            anarchy_subject = AnarchySubject(resource)
            AnarchySubject.cache_put(anarchy_subject)
            return anarchy_subject
        except kubernetes.client.rest.ApiException as e:
            if e.status == 404:
                return None
            else:
                raise

    @staticmethod
    def get_pending(runner_queue_name, runtime):
        """Get AnarchySubject with pending events or return None"""
        return AnarchySubjectRunQueue.get(runner_queue_name, runtime)

    def __init__(self, resource):
        """Initialize AnarchySubject from resource object data."""
        self.metadata = resource['metadata']
        self.spec = resource['spec']
        self.status = resource.get('status', None)
        self.__init_event_properties()
        self.__sanity_check()

    def __init_event_properties(self):
        # Last activity on subject, used to manage caching
        self.last_active = 0
        # Lock for managing pending events
        self.event_lock = threading.Lock()
        # Dictionary of active AnarchyEvents for this AnarchySubject
        self.active_events = {}
        # Current running AnarchyEvent name or AnarchyEvent waiting for retry
        self.current_event = None
        # Queue of AnarchyEvent names waiting to be run
        self.event_queue = queue.Queue()

    def __sanity_check(self):
        assert 'governor' in self.spec, \
            'subjects must define governor'

    @property
    def name(self):
        return self.metadata['name']

    @property
    def delete_started(self):
        return 'deleteHandlersStarted' in self.status

    @property
    def governor_name(self):
        return self.spec['governor']

    @property
    def is_pending_delete(self):
        return 'deletionTimestamp' in self.metadata

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

    def add_finalizer(self, runtime):
        finalizers = self.metadata.get('finalizers', [])
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': finalizers + [runtime.operator_domain] } }
        )

    def enqueue_event(self, anarchy_event, runtime):
        """Add event to queue or update event definition in queue if already present"""
        self.last_active = time.time()
        self.event_lock.acquire()
        try:
            anarchy_event_name = anarchy_event.name
            event_is_new = anarchy_event_name not in self.active_events
            self.active_events[anarchy_event_name] = anarchy_event
            if event_is_new:
                if self.current_event:
                    self.event_queue.put(anarchy_event_name)
                else:
                    self.current_event = anarchy_event_name
                    self.put_in_job_queue(runtime)
        finally:
            self.event_lock.release()

    def event_update(self, anarchy_event, runtime):
        self.last_active = time.time()
        self.event_lock.acquire()
        try:
            self.active_events[anarchy_event.name] = anarchy_event
        finally:
            self.event_lock.release()

    def get_governor(self, runtime):
        governor = AnarchyGovernor.get(self.spec['governor'])
        if not governor:
            operator_logger.error('Unable to find governor %s', self.governor_name)
        return governor

    def get_event_to_run(self, runtime):
        return self.active_events[self.current_event]

    def handle_create(self, runtime):
        self.add_finalizer(runtime)
        self.process_subject_event_handlers(runtime, 'create')

    def handle_delete(self, runtime):
        if self.delete_started:
            return
        self.record_delete_started(runtime)
        event_handled = self.process_subject_event_handlers(runtime, 'delete')
        if not event_handled:
            self.remove_finalizer(runtime)

    def handle_update(self, runtime):
        self.process_subject_event_handlers(runtime, 'update')

    def put_in_job_queue(self, runtime):
        operator_logger.info('Putting AnarchySubject %s in run queue', self.name)
        # FIXME - Allow for other job queues
        AnarchySubjectRunQueue.put('default', self.name, runtime)

    def process_action_event_handlers(self, runtime, action, event_data, event_name):
        governor = self.get_governor(runtime)
        if governor:
            governor.process_action_event_handlers(runtime, self, action, event_data, event_name)

    def process_subject_event_handlers(self, runtime, event_name):
        governor = self.get_governor(runtime)
        if governor:
            return governor.process_subject_event_handlers(runtime, self, event_name)

    def record_delete_started(self, runtime):
        runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def remove_finalizer(self, runtime):
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': None } }
        )

    def remove_active_event(self, anarchy_event, runtime):
        anarchy_event_name = anarchy_event.name
        self.event_lock.acquire()
        try:
            try:
                del self.active_events[anarchy_event_name]
                operator_logger.info('Removed AnarchyEvent %s from active events', anarchy_event_name)
            except KeyError:
                operator_logger.debug('Removed AnarchyEvent %s was not in active events', anarchy_event_name)

            if self.current_event == anarchy_event_name:
                operator_logger.info('Removing current AnarchyEvent %s for AnarchySubject %s', anarchy_event_name, self.name)
                self.current_event = None
                while True:
                    try:
                        next_event_name = self.event_queue.get_nowait()
                        if next_event_name in self.active_events:
                            operator_logger.debug('New current AnarchyEvent is %s for AnarchySubject %s', next_event_name, self.name)
                            self.current_event = next_event_name
                            self.put_in_job_queue(runtime)
                            break
                        else:
                            operator_logger.warn('AnarchyEvent %s for AnarchySubject %s removed before processing', next_event_name, self.name)
                    except queue.Empty:
                        break
        finally:
            self.event_lock.release()

    def run_queue_release(self):
        AnarchySubjectRunQueue.release(self.name)

    def start_action(self, runtime, anarchy_action):
        if self.current_event:
            operator_logger.info(
                'Deferring AnarchyAction %s on AnarchySubject %s due to AnarchyEvent processing',
                anarchy_action.name, self.name
            )
            return False
        governor = self.get_governor(runtime)
        if governor:
            operator_logger.debug(
                'Starting AnarchyAction %s on AnarchySubject %s',
                anarchy_action.name, self.name
            )
            governor.start_action(runtime, self, anarchy_action)
            return True
        else:
            operator_logger.warn(
                "Unable to find AnarchyGovernor %s for AnarchySubject %s!",
                self.governor_name, self.name
            )
            return False
