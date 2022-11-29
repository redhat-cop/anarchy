import kopf
import kubernetes_asyncio
import logging

from datetime import datetime, timezone

from anarchy import Anarchy
from anarchycachedkopfobject import AnarchyCachedKopfObject
from random_string import random_string

import anarchygovernor
import anarchyrun
import anarchysubject

class AnarchyAction(AnarchyCachedKopfObject):
    cache = {}
    kind = 'AnarchyAction'
    plural = 'anarchyactions'

    @property
    def action(self):
        return self.spec['action']

    @property
    def after_timestamp(self):
        return self.spec.get('after', datetime.now(timezone.utc).strftime('%FT%TZ'))

    @property
    def after_datetime(self):
        return datetime.strptime(self.after_timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

    @property
    def cleanup_after_datetime(self):
        anarchy_governor = self.get_governor()
        return self.finished_datetime + anarchy_governor.remove_finished_actions_after_timedelta

    @property
    def finished_datetime(self):
        return datetime.strptime(self.finished_timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

    @property
    def finished_timestamp(self):
        return self.status['finishedTimestamp']

    @property
    def governor_name(self):
        return self.spec.get('governorRef', {}).get('name')

    @property
    def governor_ref(self):
        return self.spec.get('governorRef')

    @property
    def is_delete_handler(self):
        return Anarchy.delete_handler_label in self.labels

    @property
    def is_finished(self):
        return 'finishedTimestamp' in self.status

    @property
    def is_labeled_for_cancelation(self):
        return Anarchy.canceled_label in self.labels

    @property
    def is_runnning(self):
        return self.status.get('state') == 'running'

    @property
    def is_started(self):
        return 'runScheduled' in self.status

    @property
    def run_name(self):
        return self.status.get('runRef', {}).get('name')

    @property
    def state(self):
        return self.status.get('state', 'new')

    @property
    def subject_name(self):
        return self.subject_ref['name']

    @property
    def subject_ref(self):
        return self.spec.get('subjectRef')

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def get_governor(self):
        return anarchygovernor.AnarchyGovernor.get(self.governor_name)

    async def cancel(self):
        """
        Mark AnarchyAction as canceled.

        When marked canceled the AnarchyAction may still be executing. An
        AnarhyRun associated with the AnarchyAction must finish the action.
        """
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.canceled_label.replace('/', '~1')}",
            "value": "",
        }])
        if self.state not in ('run pending', 'running'):
            await self.finish('canceled')

    async def check_start(self):
        """
        Check if action should start and create run if so.
        """
        anarchy_subject = await self.get_subject()
        anarchy_governor = anarchy_subject.get_governor()
        await anarchy_subject.check_set_active_action(self)
        if anarchy_subject.active_action_name != self.name:
            return
        if not anarchy_governor.has_action(self.action):
            raise kopf.TemporaryError(
                f"{self} cannot run {anarchy_governor} does not support action {self.action}",
                delay = 60,
            )
        anarchy_run = await self.create_anarchy_run()
        logging.info(f"Created {anarchy_run.runner_state} {anarchy_run} to start {self}")

    async def create_anarchy_run(self):
        anarchy_run = await anarchyrun.AnarchyRun.create(
            anarchy_action = self,
            handler = {
                "type": "action",
            }
        )
        await self.merge_patch_status({
            "runRef": anarchy_run.as_reference(),
            "runScheduled": datetime.now(timezone.utc).strftime('%FT%TZ'),
            "state": f"run {anarchy_run.runner_state}",
        })
        return anarchy_run

    async def finish(self, result_status):
        if self.is_finished:
            return
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/finishedTimestamp",
            "value": datetime.now(timezone.utc).strftime('%FT%TZ'),
        }, {
            "op": "add",
            "path": "/status/state",
            "value": result_status,
        }])
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.finished_label.replace('/', '~1')}",
            "value": result_status.replace(' ', '-'),
        }])

        anarchy_subject = await self.get_subject()
        await anarchy_subject.remove_action_from_status(self)

    async def get_subject(self):
        try:
            return await anarchysubject.AnarchySubject.get(self.subject_name)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise

    async def handle_create(self, anarchy_subject):
        await self.initialize(anarchy_subject)
        anarchy_subject = await self.get_subject()
        await anarchy_subject.add_action_to_status(self)

    async def handle_delete(self, anarchy_subject):
        try:
            anarchy_subject = await anarchysubject.AnarchySubject.get(self.subject_name)
            await anarchy_subject.remove_action_from_status(self)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise
        self.remove_from_cache()

    async def handle_resume(self, anarchy_subject):
        await self.initialize(anarchy_subject)

        if self.is_finished:
            if anarchy_subject.has_action_in_status(self):
                logging.warning(f"{self} is finisished but was still in {anarchy_subject} status.")
                await anarchy_subject.remove_action_from_status(self)
        elif not anarchy_subject.has_action_in_status(self):
            logging.warning(f"Adding {self} to {anarchy_subject} status, but it should have already been there?")
            await anarchy_subject.add_action_to_status(self)

    async def handle_update(self, anarchy_subject):
        anarchy_subject = await self.get_subject()
        if self.is_finished:
            if anarchy_subject.has_action_in_status(self):
                logging.info(f"{self} finished")
                await anarchy_subject.remove_action_from_status(self)
        elif self.is_labeled_for_cancelation and self.state not in ('run pending', 'running'):
            await self.finish('canceled')
        elif not anarchy_subject.has_action_in_status(self):
            logging.warning(f"{self} was mising from {anarchy_subject} status")
            await anarchy_subject.add_action_to_status(self)

    async def initialize(self, anarchy_subject):
        anarchy_governor = anarchy_subject.get_governor()
        patch = []
        governor_reference = anarchy_governor.as_reference()

        if not self.labels:
            patch.append({
                "op": "add",
                "path": "/metadata/labels",
                "value": {
                    Anarchy.action_label: self.action,
                    Anarchy.governor_label: anarchy_governor.name,
                    Anarchy.subject_label: self.subject_name,
                }
            })
        else:
            if not Anarchy.action_label in self.labels:
                patch.append({
                    "op": "add",
                    "path": f"/metadata/labels/{Anarchy.action_label.replace('/', '~1')}",
                    "value": self.action,
                })
            if not Anarchy.governor_label in self.labels:
                patch.append({
                    "op": "add",
                    "path": f"/metadata/labels/{Anarchy.governor_label.replace('/', '~1')}",
                    "value": self.governor_name,
                })
            if not Anarchy.subject_label in self.labels:
                patch.append({
                    "op": "add",
                    "path": f"/metadata/labels/{Anarchy.subject_label.replace('/', '~1')}",
                    "value": self.subject_name,
                })

        if self.governor_ref != governor_reference:
            patch.append({
                "op": "add",
                "path": "/spec/governorRef",
                "value": governor_reference,
            })
        subject_reference = anarchy_subject.as_reference()
        if self.subject_ref != subject_reference:
            patch.append({
                "op": "add",
                "path": "/spec/subjectRef",
                "value": subject_reference,
            })

        owner_references = [anarchy_subject.as_owner_ref()]
        if self.metadata.get('ownerReferences') != owner_references:
            patch.append({
                "op": "add",
                "path": "/metadata/ownerReferences",
                "value": owner_references,
            })
        if 'callbackToken' not in self.spec:
            patch.append({
                "op": "add",
                "path": "/spec/callbackToken",
                "value": random_string(32),
            })
        if patch:
            logging.info("Set references for {self}")
            await self.json_patch(patch)

    async def manage(self, anarchy_subject):
        """
        Called by daemon loop to periodically manage this action.
        """
        if self.is_finished:
            return await self.manage_finished(anarchy_subject)
        return await self.manage_active(anarchy_subject)

    async def manage_active(self, anarchy_subject):
        await anarchy_subject.add_action_to_status(self)

        if self.state in ('run pending', 'running'):
            pass
        elif self.is_labeled_for_cancelation:
            await self.finish('canceled')
        elif self.is_started:
            pass
        else:
            seconds_to_start = (
                self.after_datetime - datetime.now(timezone.utc)
            ).total_seconds()
            if seconds_to_start <= 0:
                await self.check_start()
                return 1
            if seconds_to_start < Anarchy.action_check_interval:
                return seconds_to_start

        return Anarchy.action_check_interval

    async def manage_finished(self, anarchy_subject):
        await anarchy_subject.remove_action_from_status(self)
        if self.cleanup_after_datetime <= datetime.now(timezone.utc):
            await self.delete()
        return Anarchy.action_check_interval

    async def reschedule(self, after=None, vars={}):
        patch = [{
            "op": "add",
            "path": "/status/state",
            "value": "scheduled",
        }]

        if self.is_started:
            patch.append({
                "op": "remove",
                "path": "/status/runScheduled"
            })

        await self.json_patch_status(patch)

        await self.json_patch([{
            "op": "add",
            "path": "/spec/after",
            "value": after or datetime.now(timezone.utc).strftime('%FT%TZ'),
        }, {
            "op": "add",
            "path": "/spec/vars",
            "value": {**self.vars, **vars}
        }])

    async def set_state(self, state):
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/state",
            "value": state,
        }])
