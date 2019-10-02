from datetime import datetime
import logging
import os
import queue
import threading
import time
import uuid

cache_age_limit = int(os.environ.get('ANARCHY_SUBJECT_CACHE_AGE_LIMIT', 600))
logger = logging.getLogger('anarchy')

from anarchygovernor import AnarchyGovernor

class AnarchySubject(object):
    """AnarchySubject class"""

    # Cache of active AnarchySubjects
    cache = {}
    # Queue of AnarchySubjects with pending events
    anarchy_subject_job_queue = queue.Queue()
    anarchy_subject_job_set = set()

    @staticmethod
    def anarchy_subject_job_queue_put(anarchy_subject_name):
        if anarchy_subject_name not in AnarchySubject.anarchy_subject_job_set:
            AnarchySubject.anarchy_subject_job_set.add(anarchy_subject_name)
            AnarchySubject.anarchy_subject_job_queue.put(anarchy_subject_name)

    @staticmethod
    def anarchy_subject_job_queue_get():
        anarchy_subject_name = AnarchySubject.anarchy_subject_job_queue.get()
        AnarchySubject.anarchy_subject_job_set.remove(anarchy_subject_name)
        return anarchy_subject_name

    @staticmethod
    def cache_put(anarchy_subject):
        anarchy_subject.last_active = time.time()
        AnarchySubject.cache[anarchy_subject.name] = anarchy_subject

    @staticmethod
    def cache_clean():
        for anarchy_subject_name in list(AnarchySubject.cache.keys()):
            anarchy_subject = AnarchySubject.cache[anarchy_subject_name]
            if not anarchy_subject.has_pending_events \
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
        else:
            resource = runtime.custom_objects_api.get_namespaced_custom_object(
                runtime.operator_domain, 'v1', runtime.operator_namespace,
                'anarchysubjects', name
            )
            anarchy_subject = AnarchySubject(resource)
            AnarchySubject.cache_put(anarchy_subject)
        return anarchy_subject

    @staticmethod
    def get_pending(runtime):
        """Get AnarchySubject with pending events, blocks if none pending"""
        while True:
            anarchy_subject_name = AnarchySubject.anarchy_subject_job_queue_get()
            anarchy_subject = AnarchySubject.get(anarchy_subject_name, runtime)
            return anarchy_subject

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
        self.pending_event_lock = threading.Lock()
        # Dictionary of active AnarchyEvents for this AnarchySubject
        self.pending_events = {}
        # Queue of AnarchyEvent names waiting to be run
        self.pending_event_queue = queue.Queue()
        # Current running AnarchyEvent name or AnarchyEvent waiting for retry
        self.running_event = None
        # Tracking of whether subject is already queued for processing
        self.has_job_queued_or_running = False

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
    def has_pending_events(self):
        if self.running_event or self.pending_events:
            return True
        return False

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

    def add_finalizer(self, runtime, logger):
        finalizers = self.metadata.get('finalizers', [])
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': finalizers + [runtime.operator_domain] } }
        )

    def enqueue_event(self, anarchy_event, event_is_running, runtime):
        """Add event to queue or update event definition in queue if already present"""
        self.last_active = time.time()
        self.pending_event_lock.acquire()
        try:
            anarchy_event_name = anarchy_event.name
            event_is_new = anarchy_event_name not in self.pending_events
            self.pending_events[anarchy_event_name] = anarchy_event
            if event_is_running:
                if not self.running_event:
                    self.running_event = anarchy_event_name
                    runtime.logger.info(
                        'Registered running AnarchyEvent %s', anarchy_event_name
                    )
                elif self.running_event == anarchy_event_name:
                    runtime.logger.info(
                        'Updated running AnarchyEvent %s', anarchy_event_name
                    )
                else:
                    runtime.logger.warning(
                        'Found running AnarchyEvent %s but expected %s',
                        anarchy_event_name, self.running_event
                    )
            elif anarchy_event_name == self.running_event:
                if not self.has_job_queued_or_running:
                    runtime.logger.info('Requeued AnarchyEvent %s', anarchy_event_name)
            elif event_is_new:
                runtime.logger.info('Queued AnarchyEvent %s', anarchy_event_name)
                self.pending_event_queue.put(anarchy_event_name)

            if self.has_pending_events and not self.has_job_queued_or_running:
                self.put_in_job_queue(runtime)
        finally:
            self.pending_event_lock.release()

    def get_governor(self, runtime):
        governor = AnarchyGovernor.get(self.spec['governor'])
        if not governor:
            runtime.logger.error('Unable to find governor %s', self.governor_name)
        return governor

    def get_event_to_run(self, runtime):
        self.pending_event_lock.acquire()
        try:
            # Running event will already be set if previous attempt failed
            if self.running_event and self.running_event in self.pending_events:
                self.last_active = time.time()
                return self.pending_events[self.running_event]

            while True:
                self.running_event = self.pending_event_queue.get_nowait()
                if self.running_event in self.pending_events:
                    self.last_active = time.time()
                    return self.pending_events[self.running_event]
        except queue.Empty:
            runtime.logger.warning(
                'Attempted to get pending event from AnarchySubject %s when no events pending!',
                self.name
            )
            return None
        finally:
            self.pending_event_lock.release()

    def handle_create(self, runtime, logger):
        self.add_finalizer(runtime, logger)
        self.process_subject_event_handlers(runtime, 'create', logger)

    def handle_delete(self, runtime, logger):
        if self.delete_started:
            return
        self.record_delete_started(runtime, logger)
        event_handled = self.process_subject_event_handlers(runtime, 'delete', logger)
        if not event_handled:
            self.remove_finalizer(runtime, logger)

    def handle_update(self, runtime, logger):
        self.process_subject_event_handlers(runtime, 'update', logger)

    def put_in_job_queue(self, runtime):
        runtime.logger.info('Putting AnarchySubject %s in job queue', self.name)
        self.has_job_queued_or_running = True
        AnarchySubject.anarchy_subject_job_queue_put(self.name)

    def process_action_event_handlers(self, runtime, action, event_data, event_name):
        governor = self.get_governor(runtime)
        if governor:
            governor.process_action_event_handlers(runtime, self, action, event_data, event_name)

    def process_subject_event_handlers(self, runtime, event_name, logger):
        governor = self.get_governor(runtime)
        if governor:
            return governor.process_subject_event_handlers(runtime, self, event_name, logger)

    def record_delete_started(self, runtime, logger):
        runtime.custom_objects_api.patch_namespaced_custom_object_status(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'status': {'deleteHandlersStarted': datetime.utcnow().strftime('%FT%TZ') } }
        )

    def remove_finalizer(self, runtime, logger):
        runtime.custom_objects_api.patch_namespaced_custom_object(
            runtime.operator_domain, 'v1', runtime.operator_namespace, 'anarchysubjects', self.name,
            {'metadata': {'finalizers': None } }
        )

    def remove_pending_event(self, anarchy_event, runtime):
        anarchy_event_name = anarchy_event.name
        self.pending_event_lock.acquire()
        try:
            if self.running_event == anarchy_event_name:
                runtime.logger.info('Removing running AnarchyEvent %s', anarchy_event_name)
                self.running_event = None
                self._remove_pending_event(anarchy_event_name, runtime)
                if self.has_pending_events and not self.has_job_queued_or_running:
                    self.put_in_job_queue(runtime)
            else:
                runtime.logger.info('Removing AnarchyEvent %s', anarchy_event_name)
                self._remove_pending_event(anarchy_event_name, runtime)
        finally:
            self.pending_event_lock.release()

    def _remove_pending_event(self, anarchy_event_name, runtime):
        try:
            del self.pending_events[anarchy_event_name]
        except KeyError:
            runtime.logger.warning('AnarchyEvent %s was not in pending events', anarchy_event_name)
            pass

    def start_action(self, runtime, anarchy_action):
        if self.has_pending_events:
            runtime.logger.info(
                'Deferring AnarchyAction %s on AnarchySubject %s due to pending AnarchyEvents',
                anarchy_action.name, self.name
            )
            return False
        governor = self.get_governor(runtime)
        if governor:
            runtime.logger.debug(
                'Starting AnarchyAction %s on AnarchySubject %s due to pending AnarchyEvents',
                anarchy_action.name, self.name
            )
            governor.start_action(runtime, self, anarchy_action)
            return True
        else:
            runtime.logger.warning(
                "Unable to find AnarchyGovernor %s for AnarchySubject %s!",
                self.governor_name, self.name
            )
            return False
