import kopf
import kubernetes_asyncio
import logging

from datetime import datetime, timezone

from anarchy import Anarchy
from anarchycachedkopfobject import AnarchyCachedKopfObject

import anarchyaction
import anarchygovernor
import anarchysubject

class AnarchyRun(AnarchyCachedKopfObject):
    cache = {}
    kind = 'AnarchyRun'
    plural = 'anarchyruns'

    @classmethod
    def have_unfinished_runs_for_action(cls, anarchy_action):
        for anarchy_run in cls.cache.values():
            if anarchy_run.action_name == anarchy_action.name \
            and not anarchy_run.is_finished:
                return True
        return False

    @classmethod
    async def create(cls,
        handler,
        anarchy_action = None,
        anarchy_subject = None,
    ):
        if anarchy_action:
            anarchy_subject = await anarchy_action.get_subject()
        anarchy_governor = anarchy_subject.get_governor()

        labels = {
            Anarchy.governor_label: anarchy_governor.name,
            Anarchy.runner_label: 'queued' if anarchy_subject.has_active_runs else 'pending',
            Anarchy.subject_label: anarchy_subject.name,
        }
        # Mark this AnarchyRun for delete handling.
        if anarchy_subject.is_deleting:
            labels[Anarchy.delete_handler_label] = ''

        if 'name' in handler:
            labels[Anarchy.event_label] = handler['name']

        run_spec = {
            "governor": anarchy_governor.as_reference(),
            "handler": handler,
            "subject": {
                "vars": anarchy_subject.vars,
                **anarchy_subject.as_reference(),
            }
        }

        if anarchy_action:
            owner = anarchy_action
            labels[Anarchy.action_label] = anarchy_action.name
            run_spec['action'] = anarchy_action.as_reference()
            if 'name' in handler:
                generate_name = f"{anarchy_action.name}-{handler['name']}-"
            else:
                generate_name = f"{anarchy_action.name}-"
        else:
            owner = anarchy_subject
            generate_name = f"{anarchy_subject.name}-{handler['name']}-"

        definition = await Anarchy.custom_objects_api.create_namespaced_custom_object(
            group = Anarchy.domain,
            namespace = Anarchy.namespace,
            plural = 'anarchyruns',
            version = Anarchy.version,
            body = {
                "apiVersion": Anarchy.api_version,
                "kind": "AnarchyRun",
                "metadata": {
                    "generateName": generate_name,
                    "labels": labels,
                    "namespace": Anarchy.namespace,
                    "ownerReferences": [owner.as_owner_ref()],
                },
                "spec": run_spec,
            }
        )
        anarchy_run = cls.load_definition(definition)
        await anarchy_subject.add_run_to_status(anarchy_run)
        return anarchy_run

    @property
    def action_name(self):
        if 'action' in self.spec:
            return self.spec['action']['name']

    @property
    def cleanup_after_datetime(self):
        anarchy_governor = self.get_governor()
        return self.run_post_datetime + anarchy_governor.remove_successful_runs_after_timedelta

    @property
    def continue_action(self):
        return self.status.get('result', {}).get('continueAction')

    @property
    def delete_subject(self):
        return 'deleteSubject' in self.status.get('result', {})

    @property
    def delete_subject_finalizers(self):
        return self.status.get('result', {}).get('deleteSubject', {}).get('removeFinalizers', False)

    @property
    def finish_action(self):
        return self.status.get('result', {}).get('finishAction')

    @property
    def finish_action_state(self):
        return self.status.get('result', {}).get('finishAction', {}).get('state', 'successful')

    @property
    def governor_name(self):
        return self.spec['governor']['name']

    @property
    def has_action(self):
        return 'action' in self.spec

    @property
    def is_delete_handler(self):
        return Anarchy.delete_handler_label in self.labels

    @property
    def is_failed(self):
        return self.runner_state == 'failed'

    @property
    def is_labeled_for_cancelation(self):
        """
        Cancel label may be set on run to indicate that it should be skipped and not retried.
        """
        return Anarchy.canceled_label in self.labels

    @property
    def is_lost(self):
        return self.runner_state == 'lost'

    @property
    def is_finished(self):
        return self.runner_state in ('successful', 'canceled')

    @property
    def is_pending_or_running(self):
        return self.runner_state not in ('queued', 'failed', 'lost', 'canceled', 'successful')

    @property
    def result_status(self):
        return self.status.get('result', {}).get('status')

    @property
    def result_status_message(self):
        return self.status.get('result', {}).get('statusMessage')

    @property
    def retry_after_datetime(self):
        retry_after_timestamp = self.status.get('retryAfter')
        if retry_after_timestamp:
            return datetime.strptime(retry_after_timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        else:
            return datetime.now(timezone.utc)

    @property
    def run_post_datetime(self):
        ts = self.run_post_timestamp
        if ts:
            return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

    @property
    def run_post_timestamp(self):
        return self.status.get('runPostTimestamp')

    @property
    def runner_state(self):
        return self.labels.get(Anarchy.runner_label)

    @property
    def subject_name(self):
        return self.spec['subject']['name']

    def get_governor(self):
        return anarchygovernor.AnarchyGovernor.get(self.governor_name)

    async def cancel(self):
        """
        Mark AnarchyRun as canceled.

        Immediately cancel from queued or retry states, apply canceled label otherwise.
        """
        if self.runner_state in ('failed', 'lost', 'queued'):
            await self.finish('canceled')
        elif self.is_pending_or_running:
            await self.json_patch([{
                "op": "add",
                "path": f"/metadata/labels/{Anarchy.canceled_label.replace('/', '~1')}",
                "value": "",
            }])

    async def finish(self, state):
        await self.json_patch_status([{
            "op": "add",
            "path": "/status/runPostTimestamp",
            "value": datetime.now(timezone.utc).strftime('%FT%TZ'),
        }])
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.finished_label.replace('/', '~1')}",
            "value": state,
        }, {
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": state,
        }])

    async def get_action(self):
        if not self.has_action:
            return
        try:
            return await anarchyaction.AnarchyAction.get(self.action_name)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status == 404:
                return
            else:
                raise

    async def get_subject(self):
        try:
            return await anarchysubject.AnarchySubject.get(self.subject_name)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise

    async def handle_canceled(self, anarchy_subject, anarchy_action):
        if anarchy_action and self.name == anarchy_action.run_name:
            await anarchy_action.finish('canceled')
        await anarchy_subject.remove_run_from_status(self)

    async def handle_delete(self, anarchy_subject, anarchy_action):
        try:
            await anarchy_subject.remove_run_from_status(self)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status != 404:
                raise

    async def handle_failed(self, anarchy_subject, anarchy_action):
        await anarchy_subject.set_run_status(self.result_status, self.result_status_message)
        if anarchy_action:
            if anarchy_action.is_labeled_for_cancelation:
                logging.info(f"Canceling {self} after failed run for canceled {anarchy_action}")
                await self.finish('canceled')
                await anarchy_action.finish('canceled')
            else:
                await anarchy_action.set_state('run failed')

    async def handle_lost(self, anarchy_subject, anarchy_action):
        await anarchy_subject.set_run_status('lost')
        if anarchy_action:
            if anarchy_action.is_labeled_for_cancelation:
                logging.info(f"Canceling {self} after lost run for canceled {anarchy_action}")
                await self.finish('canceled')
                await anarchy_action.finish('canceled')
            else:
                await anarchy_action.set_state('run lost')

    async def handle_pending(self, anarchy_subject, anarchy_action):
        anarchy_action = await self.get_action()
        if anarchy_action:
            await anarchy_action.set_state('run pending')

    async def handle_running(self, anarchy_subject, anarchy_action):
        anarchy_action = await self.get_action()
        if anarchy_action:
            await anarchy_action.set_state('running')

    async def handle_success(self, anarchy_subject, anarchy_action):
        await anarchy_subject.set_run_status('successful')

        if self.delete_subject_finalizers:
            if not anarchy_subject.is_deleting:
                await anarchy_subject.delete()
            await anarchy_subject.remove_finalizers()
            return

        if anarchy_action:
            anarchy_governor = self.get_governor()
            action_config = anarchy_governor.get_action_config(anarchy_action.action)
            if not action_config:
                raise kopf.TemporaryError(
                    f"{anarchy_action} action {anarchy_action.action} has no configuration in {anarchy_governor}"
                )

            if self.continue_action:
                if anarchy_action.is_labeled_for_cancelation:
                    logging.info(f"Ignoring continue action from {self} on canceled {anarchy_action}")
                if anarchy_action.is_finished:
                    logging.info(f"Ignoring continue action from {self} on finished {anarchy_action}")
                else:
                    await anarchy_action.reschedule(**self.continue_action)

            elif self.finish_action:
                await anarchy_action.finish(self.finish_action_state)

            elif not AnarchyRun.have_unfinished_runs_for_action(anarchy_action):
                if action_config.finish_on_successful_run:
                    logging.info(f"Finishing {anarchy_action} on successful run of {self}")
                    await anarchy_action.finish('successful')
                elif not action_config.has_callback_handlers:
                    logging.warning(f"Finishing {anarchy_action} as it has no further runs or callback handlers")
                    await anarchy_action.finish('successful')

        await anarchy_subject.remove_run_from_status(self)

        if self.delete_subject and not anarchy_subject.is_deleting:
            await anarchy_subject.delete()

        if self.is_delete_handler:
            if await anarchy_subject.check_complete_delete():
                return

    async def handle_resume(self, anarchy_subject, anarchy_action):
        if self.is_finished:
            if anarchy_subject.active_run_name == self.name:
                logging.warning(f"{self} is successful but still listed as the active run in {anarchy_subject}")
            elif anarchy_subject.has_run_in_status(self):
                logging.warning(f"{self} is successful but still listed in {anarchy_subject}?")
            await anarchy_subject.remove_run_from_status(self)
        else:
            if not anarchy_subject.has_run_in_status(self):
                logging.warning(f"{self} is {self.runner_state} but was not listed in {anarchy_subject}")
                await anarchy_subject.add_run_to_status(self)
            if self.runner_state == 'queued' and self.name == anarchy_subject.active_run_name:
                logging.warning(f"{self} is active run for {anarchy_subject} but was still marked as queued")
                await self.set_to_pending()

    async def manage(self, anarchy_subject, anarchy_action):
        if self.is_finished:
            return await self.manage_finished(anarchy_subject, anarchy_action)
        else:
            return await self.manage_active(anarchy_subject, anarchy_action)

    async def manage_active(self, anarchy_subject, anarchy_action):
        if self.is_pending_or_running:
            # Anything pending or running is managed by the API component
            pass
        elif anarchy_action and anarchy_action.is_labeled_for_cancelation:
            logging.info("Canceling {self} for canceled {anarchy_action}")
            await self.finish('canceled')
            await anarchy_action.finish('canceled')
        elif anarchy_action and anarchy_action.is_finished:
            logging.info("Canceling {self} for finished {anarchy_action}")
            await self.finish('canceled')
        elif self.is_labeled_for_cancelation:
            await self.finish('canceled')
        elif self.runner_state in ('failed', 'lost'):
            seconds_to_retry = (
                self.retry_after_datetime - datetime.now(timezone.utc)
            ).total_seconds()
            if seconds_to_retry < 0:
                await self.retry()
            elif seconds_to_retry < Anarchy.run_check_interval:
                return seconds_to_retry
        elif self.runner_state == 'queued':
            await anarchy_subject.add_run_to_status(self)
            if self.name == anarchy_subject.active_run_name:
                logging.info(f"{self} transition to pending")
                await self.set_to_pending()

        return Anarchy.run_check_interval

    async def manage_finished(self, anarchy_subject, anarchy_action):
        if self.cleanup_after_datetime <= datetime.now(timezone.utc):
            await self.delete()
        return Anarchy.run_check_interval

    async def retry(self):
        logging.info(f"{self} retrying")
        await self.set_to_pending()

    async def set_to_pending(self):
        await self.json_patch([{
            "op": "add",
            "path": f"/metadata/labels/{Anarchy.runner_label.replace('/', '~1')}",
            "value": "pending",
        }])
