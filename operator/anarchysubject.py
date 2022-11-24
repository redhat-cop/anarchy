import asyncio
import hashlib
import json
import kopf
import kubernetes_asyncio
import logging

from base64 import b64encode

from anarchy import Anarchy
from anarchycachedkopfobject import AnarchyCachedKopfObject

import anarchyaction
import anarchygovernor
import anarchyrun

class AnarchySubject(AnarchyCachedKopfObject):
    cache = {}
    kind = 'AnarchySubject'
    plural = 'anarchysubjects'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lock = asyncio.Lock()

    @property
    def active_action_name(self):
        ref = self.active_action_ref
        if ref:
            return ref['name']

    @property
    def active_action_ref(self):
        return self.status.get('activeAction')

    @property
    def active_run_name(self):
        ref = self.active_run_ref
        if ref:
            return ref['name']

    @property
    def active_run_ref(self):
        active_runs = self.active_runs
        if active_runs:
            return active_runs[0]

    @property
    def active_runs(self):
        return self.status.get('runs', {}).get('active', [])

    @property
    def governor_name(self):
        return self.spec.get('governor')

    @property
    def has_active_action(self):
        """
        Test if there is an action currently active either executing a run or waiting on callbacks.
        If there is no active action then a pending action can become active.
        """
        return True if self.status.get('activeAction') else False

    @property
    def has_active_action(self):
        return 'activeAction' in self.status

    @property
    def has_active_runs(self):
        return len(self.active_runs) > 0

    @property
    def has_pending_actions(self):
        return len(self.pending_actions) > 0

    @property
    def has_subject_finalizer(self):
        return Anarchy.subject_label in self.finalizers

    @property
    def pending_actions(self):
        return self.status.get('pendingActions', [])

    @property
    def spec_sha256(self):
        return b64encode(hashlib.sha256(json.dumps(
            {**self.spec}, sort_keys=True, separators=(',',':')
        ).encode('utf-8')).digest()).decode('utf-8')

    @property
    def vars(self):
        return self.spec.get('vars', {})

    def get_governor(self):
        if not self.governor_name:
            raise kopf.PermanentError("{self} spec.governor is required")
        return anarchygovernor.AnarchyGovernor.get(self.governor_name)

    def has_action_in_status(self, anarchy_action):
        if anarchy_action.name == self.active_action_name:
            return True
        for action_ref in self.pending_actions:
            if action_ref['name'] == anarchy_action.name:
                return True
        return False

    def has_run_in_status(self, anarchy_run):
        for run_ref in self.status.get('runs', {}).get('active', []):
            if run_ref['name'] == anarchy_run.name:
                return True
        return False

    async def add_action_to_status(self, anarchy_action):
        if self.has_action_in_status(anarchy_action):
            return
        entry = {
            "after": anarchy_action.after_timestamp,
            **anarchy_action.as_reference(),
        }
        if not 'pendingActions' in self.status:
            await self.json_patch_status([{
                "op": "add",
                "path": "/status/pendingActions",
                "value": [entry],
            }])
            return
        for i, item in enumerate(self.pending_actions):
            if item['after'] > anarchy_action.after_timestamp:
                await self.json_patch_status([{
                    "op": "add",
                    "path": f"/status/pendingActions/{i}",
                    "value": entry,
                }])
                break
        else:
            await self.json_patch_status([{
                "op": "add",
                "path": f"/status/pendingActions/-",
                "value": entry,
            }])

    async def add_run_to_status(self, anarchy_run):
        if self.has_run_in_status(anarchy_run):
            return
        if not 'runs' in self.status:
            await self.json_patch_status([{
                "op": "add",
                "path": f"/status/runs",
                "value": {"active": [anarchy_run.as_reference()]},
            }])
        elif not 'active' in self.status['runs']:
            await self.json_patch_status([{
                "op": "add",
                "path": f"/status/runs/active",
                "value": [anarchy_run.as_reference()],
            }])
        else:
            await self.json_patch_status([{
                "op": "add",
                "path": f"/status/runs/active/-",
                "value": anarchy_run.as_reference(),
            }])

    async def check_complete_delete(self):
        """
        Check if subject is deleting and has resolved all runs and actions.

        Called upon completion of successful run.
        """
        if not self.is_deleting:
            logging.warning("check_complete_delete called on {self} which is not deleting?")
            return

        if not self.has_active_runs \
        and not self.has_active_action \
        and not self.has_pending_actions:
            await self.remove_subject_finalizer()
            self.remove_from_cache()
            return True

    async def check_set_active_action(self, anarchy_action):
        if self.active_action_name:
            return
        if not self.pending_actions:
            raise kopf.TemporaryError(f"{self} has no pending actions when checking {anarchy_action}")
        if self.pending_actions[0]['name'] == anarchy_action.name:
            await self.json_patch_status([{
                "op": "test",
                "path": "/status/activeAction",
                "value": None,
            }, {
                "op": "test",
                "path": "/status/pendingActions/0/name",
                "value": anarchy_action.name,
            }, {
                "op": "move",
                "from": "/status/pendingActions/0",
                "path": "/status/activeAction",
            }])

    async def create_anarchy_run(self, event_name=None, event_vars=None):
        anarchy_run = await anarchyrun.AnarchyRun.create(
            anarchy_subject=self,
            handler = {
                "name": event_name,
                "type": "subjectEvent",
                "vars": event_vars,
            }
        )
        logging.info(f"Created {anarchy_run} to handle {event_name} for {self}")
        await self.add_run_to_status(anarchy_run)
        return anarchy_run

    async def delete_backwards_compatibility_fixup(self):
        """
        Previous versions of anarchy did not have the same model for delete handling.

        For compatibilty during upgrade, add deleting label to any runs created after deletion timestamp.
        """
        for run_ref in self.active_runs:
            run_name = run_ref['name']
            try:
                anarchy_run = await anarchyrun.AnarchyRun.get(run_name)
                if anarchy_run.creation_timestamp >= self.deletion_timestamp \
                and not anarchy_run.is_delete_handler:
                    logging.info(f"Applying deleting label to {anarchy_run}")
                    await anarchy_run.json_patch([{
                        "op": "add",
                        "path": f"/metadata/labels/{Anarchy.delete_handler_label.replace('/', '~1')}",
                        "value": "",
                    }])
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    logging.warning(f"Could not find AnarchyRun {run_name} during deletion fixup")
                else:
                    raise

        if self.has_active_action:
            try:
                anarchy_action = await self.get_active_action()
                if anarchy_action.creation_timestamp >= self.deletion_timestamp:
                    if not anarchy_action.is_delete_handler:
                        logging.info(f"Applying deleting label to {anarchy_action}")
                        await anarchy_action.json_patch([{
                            "op": "add",
                            "path": f"/metadata/labels/{Anarchy.delete_handler_label.replace('/', '~1')}",
                            "value": "",
                        }])
                else:
                    await anarchy_action.cancel()
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    logging.warning(f"Could not find AnarchyAction {self.active_action_name} during deletion fixup for {self}")
                else:
                    raise

        for action_ref in self.pending_actions:
            anarchy_action_name = action_ref['name']
            try:
                anarchy_action = await anarchyaction.AnarchyAction.get(anarchy_action_name)
                if anarchy_action.creation_timestamp >= self.deletion_timestamp:
                    if not anarchy_action.is_delete_handler:
                        logging.info(f"Applying deleting label to {anarchy_action}")
                        await anarchy_action.json_patch([{
                            "op": "add",
                            "path": f"/metadata/labels/{Anarchy.delete_handler_label.replace('/', '~1')}",
                            "value": "",
                        }])
                else:
                    await anarchy_action.cancel()
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    logging.warning(f"Could not find pending AnarchyAction {anarchy_action_name} during deletion fixup for {self}")
                else:
                    raise

    async def get_active_action(self):
        action_name = self.active_action_name
        if action_name:
            return await anarchyaction.AnarchyAction.get(action_name)

    async def handle_create(self):
        anarchy_governor = self.get_governor()
        await self.initialize_metadata()
        await self.initialize_status()
        logging.info(f"{self} initialized")
        if anarchy_governor.has_create_handler:
            await self.create_anarchy_run(event_name='create')

    async def handle_delete(self):
        """
        Create AnarchyRun to handle delete or remove finalizers if not.
        """
        anarchy_governor = self.get_governor()
        if not anarchy_governor.has_delete_handler:
            await self.remove_subject_finalizer()
            self.remove_from_cache()
            return

        if self.has_active_action:
            try:
                anarchy_action = await self.get_active_action()
                logging.info(f"Canceling {anarchy_action} on delete {self}")
                await anarchy_action.cancel()
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status != 404:
                    raise

        for action_ref in self.pending_actions:
            anarchy_action_name = action_ref['name']
            try:
                anarchy_action = await anarchyaction.AnarchyAction.get(anarchy_action_name)
                logging.info(f"Canceling {anarchy_action} on delete {self}")
                await anarchy_action.cancel()
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status != 404:
                    raise

        for run_ref in self.active_runs:
            run_name = run_ref['name']
            try:
                anarchy_run = await anarchyrun.AnarchyRun.get(run_name)
                logging.info(f"Canceling {anarchy_run} on delete {self}")
                await anarchy_run.cancel()
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status != 404:
                    raise

        await self.create_anarchy_run(event_name='delete')

    async def handle_post_delete_event(self):
        anarchy_governor = anarchygovernor.AnarchyGovernor.cache.get(self.governor_name)
        if not anarchy_governor:
            logging.info(
                f"AnarchyGovernor {self.governor_name} not found, allowing delete of {self} to complete"
            )
            await self.remove_subject_finalizer()
        elif anarchy_governor.has_delete_handler:
            await self.delete_backwards_compatibility_fixup()
            if not self.has_active_runs \
            and not self.has_active_action \
            and not self.has_pending_actions:
                logging.warning(f"{self} is deleting but has no runs or actions that could complete deletion!")
        else:
            logging.info(
                f"{anarchy_governor} no longer has a delete handler, allowing delete of {self} to complete"
            )
            await self.remove_subject_finalizer()

    async def handle_resume(self):
        await self.initialize_metadata()
        await self.manage_status()

    async def handle_update(self, previous_state):
        anarchy_governor = self.get_governor()
        await self.initialize_metadata()
        if not anarchy_governor.has_update_handler:
            return
        if self.spec_sha256 == self.annotations.get(Anarchy.spec_sha256_annotation):
            logging.debug("{self} skipping update handling")
            return

        await self.create_anarchy_run(
            event_name = 'update',
            event_vars = {"anarchy_subject_previous_state": previous_state},
        )

        await self.merge_patch({
            "metadata": {
                "annotations": {
                    Anarchy.spec_sha256_annotation: self.spec_sha256
                }
            }
        })

    async def initialize_metadata(self):
        '''
        Set subject finalizer and governor label
        '''
        patch = []
        governor = self.get_governor()

        if governor.has_delete_handler:
            # If the governor has a delete handler then the subject needs an
            # extra finalizer to prevent kopf from deleting the subject before
            # the handler activity can complete.
            if not self.has_subject_finalizer and not self.is_deleting:
                patch.append({
                   "op": "add",
                   "path": "/metadata/finalizers/-",
                   "value": Anarchy.subject_label,
                })
        elif self.has_subject_finalizer:
            # The governor does not have a delete handler, so no subject
            # finalizer should be present.
            patch.append({
                "op": "add",
                "path": f"/metadata/finalizers/{self.finalizers.index(Anarchy.subject_label)}",
                "value": Anarchy.subject_label,
            })

        # Apply governor label
        if not self.labels:
            patch.append({
                "op": "add",
                "path": "/metadata/labels",
                "value": {
                    Anarchy.governor_label: self.governor_name,
                }
            })
        elif not Anarchy.governor_label in self.labels:
            patch.append({
                "op": "add",
                "path": f"/metadata/labels/{Anarchy.governor_label.replace('/', '~1')}",
                "value": self.governor_name,
            })

        if not self.annotations:
            patch.append({
                "op": "add",
                "path": f"/metadata/annotations",
                "value": {
                    Anarchy.spec_sha256_annotation: self.spec_sha256
                }
            })
        elif Anarchy.spec_sha256_annotation not in self.annotations:
            patch.append({
                "op": "add",
                "path": f"/metadata/annotations/{Anarchy.spec_sha256_annotation.replace('/', '~1')}",
                "value": self.spec_sha256,
            })

        if patch:
            await self.json_patch(patch)

    async def initialize_status(self):
        governor = self.get_governor()
        patch = []
        if self.status:
            await self.manage_status()
        else:
            await self.json_patch_status([{
                "op": "add",
                "path": "/status",
                "value": {
                    "pendingActions": [],
                    "runs": {
                        "active": [],
                    },
                    "supportedActions": governor.supported_actions,
                }
            }])

    async def manage_action_in_status(self, anarchy_action):
        if anarchy_action.is_finished:
            await self.remove_action_from_status(anarchy_action)
        else:
            if not self.has_action_in_status(anarchy_action):
                logging.warning(f"{anarchy_action} was not in {self} status")
                await self.add_action_to_status(anarchy_action)

    async def manage_run_in_status(self, anarchy_run):
        if anarchy_run.is_finished:
            await self.remove_run_from_status(anarchy_run)
        else:
            if not self.has_run_in_status(anarchy_run):
                logging.warning(f"{anarchy_run} was {anarchy_run.runner_state} but not in {self} status")
                await self.add_run_to_status(anarchy_run)

    async def manage_status(self):
        governor = self.get_governor()
        patch = []
        if self.active_action_name:
            try:
                await anarchyaction.AnarchyAction.get(self.active_action_name)
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    logging.warning(f"Removing missing active AnarchyAction {self.active_action_name} from {self}")
                    patch.apppend({
                        "op": "remove",
                        "path": f"/status/activeAction",
                    })
                else:
                    raise

        if 'pendingActions' in self.status:
            for i, item in enumerate(self.pending_actions):
                action_name = item['name']
                remove_action = False
                try:
                    anarchy_action = await anarchyaction.AnarchyAction.get(action_name)
                    if anarchy_action.is_finished:
                        logging.warning(f"Removing finished {anarchy_action} from {self}")
                        remove_action = True
                except kubernetes_asyncio.client.rest.ApiException as e:
                    if e.status == 404:
                        remove_action = True
                        logging.warning(f"Removing missing AnarchyAction {action_name} from {self}")
                    else:
                        raise
                if remove_action:
                    patch.insert(0, {
                        "op": "remove",
                        "path": f"/status/pendingActions/{i}",
                    })
        else:
            patch.append({
                "op": "add",
                "path": "/status/pendingActions",
                "value": []
            })

        if 'runs' in self.status:
            for i, item in enumerate(self.active_runs):
                run_name = item['name']
                remove_run = False
                try:
                    anarchy_run = await anarchyrun.AnarchyRun.get(run_name)
                    if anarchy_run.is_finished:
                        logging.warning(f"Removing {anarchy_run.runner_state} {anarchy_run} from {self}")
                        remove_run = True
                except kubernetes_asyncio.client.rest.ApiException as e:
                    if e.status == 404:
                        removeRun = True
                        logging.warning(f"Removing missing AnarchyRun {run_name} from {self}")
                    else:
                        raise
                if remove_run:
                    patch.insert(0, {
                        "op": "remove",
                        "path": f"/status/runs/active/{i}",
                    })
        else:
            patch.append({
                "op": "add",
                "path": "/status/runs",
                "value": {
                    "active": []
                }
            })

        if self.status.get('supportedActions') != governor.supported_actions:
            patch.append({
                "op": "add",
                "path": "/status/supportedActions",
                "value": governor.supported_actions,
            })

        if patch:
            await self.json_patch_status(patch)

    async def remove_action_from_status(self, anarchy_action):
        while True:
            try:
                patch = []
                if anarchy_action.name == self.active_action_name:
                    patch.append({
                        "op": "remove",
                        "path": "/status/activeAction",
                    })
                else:
                    for i, item in enumerate(self.pending_actions):
                        if item['name'] == anarchy_action.name:
                            patch.insert(0, {
                                "op": "remove",
                                "path": f"/status/pendingActions/{i}",
                            })
                if patch:
                    await self.json_patch_status(patch)
                return
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 422:
                    await self.refresh()
                elif e.status != 404:
                    logging.error(f"Failed to apply {patch} to {self}")
                    raise

    async def remove_anarchy_finalizers(self):
        """
        Remove old subject finalizer if present
        """
        if self.has_subject_finalizer:
            await self.json_patch([{
                "op": "remove",
                "path": f"/metadata/finalizers/{self.finalizers.index(Anarchy.subject_label)}",
            }])

    async def remove_run_from_status(self, anarchy_run):
        if self.has_run_in_status(anarchy_run):
            try:
                if anarchy_run.name == self.active_run_name:
                    await self.remove_active_run_from_status(anarchy_run)
                else:
                    await self.remove_queued_run_from_status(anarchy_run)
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status != 404:
                    raise

    async def remove_active_run_from_status(self, anarchy_run):
        try:
            await self.json_patch_status([{
                "op": "test",
                "path": f"/status/runs/active/0/name",
                "value": anarchy_run.name,
            }, {
                "op": "remove",
                "path": f"/status/runs/active/0",
            }])
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status == 404:
                return
            else:
                raise
        await self.set_active_run_pending()

    async def remove_queued_run_from_status(self, anarchy_run):
        try:
            patch = []
            for i, entry in enumerate(self.status['runs']['active']):
                if entry['name'] == anarchy_run.name:
                    patch.insert(0, {
                        "op": "remove",
                        "path": f"/status/runs/active/{i}",
                    })
                    patch.insert(0, {
                        "op": "test",
                        "path": f"/status/runs/active/{i}/name",
                        "value": anarchy_run.name,
                    })
            if patch:
                await self.json_patch_status(patch)
        except kubernetes_asyncio.client.rest.ApiException as e:
            if e.status == 404:
                return
            else:
                raise

    async def remove_finalizers(self):
        await self.merge_patch({
            "metadata": {
                "finalizers": [
                    i for i in self.finalizers if i not in (Anarchy.domain, Anarchy.subject_label)
                ]
            }
        })

    async def remove_subject_finalizer(self):
        if self.has_subject_finalizer:
            await self.json_patch([{
                "op": "remove",
                "path": f"/metadata/finalizers/{self.finalizers.index(Anarchy.subject_label)}",
            }])

    async def set_run_status(self, status, status_message=None):
        await self.merge_patch_status({
            "runStatus": status,
            "runStatusMessage": status_message,
        })

    async def set_active_run_pending(self):
        while True:
            if not self.active_run_name:
                return
            try:
                anarchy_run = await anarchyrun.AnarchyRun.get(self.active_run_name)

                if self.is_deleting \
                and not anarchy_run.is_delete_handler:
                    await anarchy_run.set_to_canceled()
                    logging.info("{anarchy_run} canceled because it is not related to pending delete")
                    await self.remove_run_from_status(anarchy_run)
                else:
                    if anarchy_run.has_action:
                        anarchy_action = await anarchy_run.get_action()
                    else:
                        anarchy_action = None
                    await anarchy_run.manage(self, anarchy_action)
                    if anarchy_run.name == self.active_run_name:
                        return anarchy_run
            except kubernetes_asyncio.client.rest.ApiException as e:
                if e.status == 404:
                    logging.warning(
                        f"Attempted to set AnarchyRun {self.active_run_name} "
                        f"in {self} to pending but it was not found."
                    )
                    await self.json_patch_status([{
                        "op": "remove",
                        "path": "/status/runs/active/0",
                    }])
                else:
                    raise
