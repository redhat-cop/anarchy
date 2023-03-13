import kubernetes_asyncio
import logging

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from anarchy import Anarchy
from anarchywatchobject import AnarchyWatchObject

import anarchyaction
import anarchygovernor
import anarchyrunner
import anarchysubject
import anarchyrunnerpod

class AnarchyRun(AnarchyWatchObject):
    cache = {}
    kind = 'AnarchyRun'
    plural = 'anarchyruns'
    preload = True
    pending_run_names = []
    runner_assignments = {}
    runner_states = {'queued', 'pending', 'failed', 'lost', 'canceled', 'successful'}

    @classmethod
    def cache_remove(cls, name):
        super().cache_remove(name)
        if name in cls.pending_run_names:
            cls.pending_run_names.remove(name)
        runner_pod_name = cls.runner_assignments.pop(name, None)
        if runner_pod_name:
            logging.warning(f"Removed AnarchyRun {name} that was assigned to AnarchyRunnerPod {runner_pod_name}")

    @classmethod
    async def create(cls, handler, anarchy_action):
        """
        Create run, similar to method in operator component but simplified for
        api where runs are only created for callback events.
        """
        anarchy_subject = await anarchy_action.get_subject()
        anarchy_governor = await anarchy_subject.get_governor()

        definition = await Anarchy.custom_objects_api.create_namespaced_custom_object(
            group = Anarchy.domain,
            namespace = Anarchy.namespace,
            plural = 'anarchyruns',
            version = Anarchy.version,
            body = {
                "apiVersion": Anarchy.api_version,
                "kind": "AnarchyRun",
                "metadata": {
                    "generateName": f"{anarchy_action.name}-{handler['name']}-",
                    "labels": {
                        Anarchy.event_label: handler['name'],
                        Anarchy.action_label: anarchy_action.name,
                        Anarchy.governor_label: anarchy_governor.name,
                        Anarchy.runner_label: 'queued',
                        Anarchy.subject_label: anarchy_subject.name,
                    },
                    "namespace": Anarchy.namespace,
                    "ownerReferences": [anarchy_action.as_owner_ref()],
                },
                "spec": {
                    "action": anarchy_action.as_reference(),
                    "governor": anarchy_governor.as_reference(),
                    "handler": handler,
                    "subject": {
                        "vars": anarchy_subject.vars,
                        **anarchy_subject.as_reference(),
                    }
                }
            }
        )
        anarchy_run = cls(definition)
        await anarchy_run.cache_put()
        return anarchy_run

    @classmethod
    def get_run_assigned_to_runner_pod(cls, anarchy_runner_pod):
        for run_name, runner_pod_name in cls.runner_assignments.items():
            if runner_pod_name == anarchy_runner_pod.name:
                return cls.cache[run_name]

    @classmethod
    async def get_run_for_runner_pod(cls, anarchy_runner, anarchy_runner_pod):
        while True:
            if not cls.pending_run_names:
                return None
            anarchy_run = cls.cache[cls.pending_run_names.pop(0)]
            while anarchy_run.runner_state == 'pending':
                try:
                    await anarchy_run.assign_runner_pod(anarchy_runner, anarchy_runner_pod)
                    return anarchy_run
                except kubernetes_asyncio.client.rest.ApiException as e:
                    if e.status == 404:
                        cls.cache_remove(anarchy_run.name)
                    elif e.status == 409:
                        await anarchy_run.refetch()

    @classmethod
    async def handle_any_lost_runs(cls, anarchy_runner_pod):
        lost_run_names = []
        for run_name, runner_pod_name in cls.runner_assignments.items():
            if runner_pod_name == anarchy_runner_pod.name:
                anarchy_runner_pod.consecutive_failure_count += 1
                lost_run_names.append(run_name)
        for lost_run_name in lost_run_names:
            anarchy_run = cls.cache.get(lost_run_name)
            if not anarchy_run:
                continue
            logging.warning(f"{anarchy_run} was lost by {anarchy_runner_pod}")
            try:
                await anarchy_run.set_runner_state_lost(anarchy_runner_pod)
            except Exception as e:
                logging.exception("Error resetting {anarchy_run} to lost")

    @classmethod
    async def on_startup(cls):
        await super().on_startup()
        for anarchy_run in cls.cache.values():
            runner_pod_name = anarchy_run.runner_state
            if runner_pod_name in cls.runner_states:
                continue
            if runner_pod_name not in anarchyrunnerpod.AnarchyRunnerPod.cache:
                logging.warning("{anarchy_run} was lost by AnarchyRunner pod {runner_pod_name} on startup")
                await anarchy_run.set_runner_state_pending()

    @property
    def action_name(self):
        return self.spec.get('action', {}).get('name')

    @property
    def failure_count(self):
        return self.status.get('failures', 0)

    @property
    def governor_name(self):
        return self.spec['governor']['name']

    @property
    def is_delete_handler(self):
        return Anarchy.delete_handler_label in self.labels

    @property
    def retry_after_timedelta(self):
        if self.failure_count > 8:
            return timedelta(minutes=10)
        else:
            return timedelta(seconds=2 ** self.failure_count)

    @property
    def runner_name(self):
        return self.status.get('runner', {}).get('name')

    @property
    def runner_state(self):
        return self.labels.get(Anarchy.runner_label)

    @property
    def subject_name(self):
        return self.spec['subject']['name']

    @property
    def subject_vars(self):
        return self.spec['subject'].get('vars', {})

    def get_handler(self):
        handler = self.spec.get('handler')
        spec_vars = self.spec.get('vars', {})

        # Legacy action runs do not have handler
        if not handler:
            return {
                "type": "action",
                "vars": spec_vars,
            }

        # Runs which specify type are good to go
        if 'type' in handler:
            return handler

        if 'anarchy_event_name' in spec_vars:
            return {
                "name": spec_vars['anarchy_event_name'],
                "type": "subjectEvent",
                "vars": spec_vars,
            }
        if 'anarchy_action_callback_name' in spec_vars:
            return {
                "name": spec_vars['anarchy_action_callback_name'],
                "type": "actionCallback",
                "vars": spec_vars,
            }

    def update_pending_run_names(self):
        if self.runner_state == 'pending':
            if self.name not in self.pending_run_names:
                self.pending_run_names.append(self.name)
        elif self.name in self.pending_run_names:
            self.pending_run_names.remove(self.name)

    async def assign_runner_pod(self, anarchy_runner, anarchy_runner_pod):
        definition = deepcopy(self.definition)
        definition['metadata']['labels'][Anarchy.runner_label] = anarchy_runner_pod.name
        definition = await Anarchy.custom_objects_api.replace_namespaced_custom_object(
            body = definition,
            group = Anarchy.domain,
            name = self.name,
            namespace = self.namespace,
            plural = self.plural,
            version = Anarchy.version,
        )
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/runner",
            "value": anarchy_runner.as_reference(),
        }, {
            "op": "add",
            "path": "/status/runnerPod",
            "value": anarchy_runner_pod.as_reference(),
        }])
        logging.info(f"Assigned {self} to {anarchy_runner_pod}")

    async def cache_put(self):
        await super().cache_put()
        self.update_pending_run_names()
        await self.update_runner_assignment()

    async def export(self):
        """
        Get AnarchyRun with compatibility adjustments for deprecated schema.
        """
        ret = deepcopy(self.definition)
        ret['spec']['handler'] = self.get_handler()
        ret['spec'].pop('vars', None)
        return ret

    async def get_action(self):
        if self.action_name:
            return await anarchyaction.AnarchyAction.get(self.action_name)

    async def get_governor(self):
        return await anarchygovernor.AnarchyGovernor.get(self.governor_name)

    async def get_runner(self):
        return await anarchyrunner.AnarchyRunner.get(self.runner_name)

    async def get_subject(self):
        return await anarchysubject.AnarchySubject.get(self.subject_name)

    async def set_runner_state_failed(self, result):
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/failures",
            "value": self.failure_count + 1,
        }, {
            "op": "add",
            "path": "/status/result",
            "value": result,
        }, {
            "op": "add",
            "path": "/status/retryAfter",
            "value": (datetime.now(timezone.utc) + self.retry_after_timedelta).strftime('%FT%TZ'),
        }, {
            "op": "add",
            "path": "/status/runPostTimestamp",
            "value": datetime.now(timezone.utc).strftime('%FT%TZ'),
        }])
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": "failed",
        }])

    async def set_runner_state_lost(self, anarchy_runner_pod):
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/failures",
            "value": self.failure_count + 1,
        }, {
            "op": "add",
            "path": "/status/result",
            "value": {
                "status": "lost",
                "statusMessage": f"{anarchy_runner_pod} requested a new AnarcyhRun without posting a result!",
            }
        }, {
            "op": "add",
            "path": "/status/retryAfter",
            "value": (datetime.now(timezone.utc) + self.retry_after_timedelta).strftime('%FT%TZ'),
        }, {
            "op": "add",
            "path": "/status/runPostTimestamp",
            "value": datetime.now(timezone.utc).strftime('%FT%TZ'),
        }])
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": "lost",
        }])

    async def set_runner_state_pending(self):
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": "pending",
        }])
        runner = await self.get_runner()
        await runner.update_status()

    async def set_runner_state_successful(self, result):
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/result",
            "value": result,
        }, {
            "op": "add",
            "path": "/status/runPostTimestamp",
            "value": datetime.now(timezone.utc).strftime('%FT%TZ'),
        }])
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": "successful",
        }])

    async def update_definition(self, definition):
        await super().update_definition(definition)
        self.update_pending_run_names()
        await self.update_runner_assignment()

    async def update_runner_assignment(self):
        runner_pod_name = self.runner_state
        if runner_pod_name in self.runner_states:
            self.runner_assignments.pop(self.name, None)
            return
        if runner_pod_name in anarchyrunnerpod.AnarchyRunnerPod.cache:
            self.runner_assignments[self.name] = self.runner_state
        else:
            logging.warning(f"{self} reset to pending due to missing AnarchyRunnerPod {runner_pod_name}")
            try:
                await self.set_runner_state_pending()
            except Exception as e:
                logging.exception("Error resetting {self} to pending after missing AnarcyhRunnerPod")
