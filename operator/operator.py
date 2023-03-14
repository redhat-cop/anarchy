#!/usr/bin/env python

import asyncio
import kopf
import kubernetes_asyncio
import logging

from anarchy import Anarchy
from anarchygovernor import AnarchyGovernor
from anarchysubject import AnarchySubject
from anarchyaction import AnarchyAction
from anarchyrun import AnarchyRun
from anarchyrunner import AnarchyRunner
from configure_kopf_logging import configure_kopf_logging
from infinite_relative_backoff import InfiniteRelativeBackoff

@kopf.on.startup()
async def on_startup(settings: kopf.OperatorSettings, logger, **_):
    await Anarchy.on_startup()
    await AnarchyGovernor.preload()

    # Never give up from network errors
    settings.networking.error_backoffs = InfiniteRelativeBackoff()

    # Store last handled configuration in status
    settings.persistence.diffbase_storage = kopf.StatusDiffBaseStorage(field='status.diffBase')

    # Use operator domain as finalizer
    settings.persistence.finalizer = Anarchy.domain

    # Store progress in status. Some objects may be too large to store status in metadata annotations
    settings.persistence.progress_storage = kopf.StatusProgressStorage(field='status.kopf.progress')

    # Only create events for warnings and errors
    settings.posting.level = logging.WARNING

    # Disable scanning for crds and namespaces
    settings.scanning.disabled = True

    configure_kopf_logging()

@kopf.on.cleanup()
async def on_cleanup(**_):
    await Anarchy.on_cleanup()

@kopf.on.event(Anarchy.domain, Anarchy.version, 'anarchygovernors')
async def governor_event(event, logger, **_):
    await AnarchyGovernor.handle_event(event)


@kopf.on.create(Anarchy.domain, Anarchy.version, 'anarchysubjects')
async def subject_create(**kwargs):
    anarchy_subject = AnarchySubject.load(**kwargs)
    async with anarchy_subject.lock:
        await anarchy_subject.handle_create()

@kopf.on.delete(Anarchy.domain, Anarchy.version, 'anarchysubjects')
async def subject_delete(**kwargs):
    anarchy_subject = AnarchySubject.load(**kwargs)
    async with anarchy_subject.lock:
        await anarchy_subject.handle_delete()

@kopf.on.resume(Anarchy.domain, Anarchy.version, 'anarchysubjects')
async def subject_resume(meta, name, **kwargs):
    if 'deletionTimestamp' in meta \
    and Anarchy.domain not in meta['finalizers'] \
    and Anarchy.subject_label not in meta['finalizers']:
        logging.info(f"Ignoring functionally deleted AnarchySubject {name} on resume")
        return

    anarchy_subject = AnarchySubject.load(meta=meta, name=name, **kwargs)
    async with anarchy_subject.lock:
        await anarchy_subject.handle_resume()

@kopf.on.update(Anarchy.domain, Anarchy.version, 'anarchysubjects')
async def subject_update(new, old, **kwargs):
    anarchy_subject = AnarchySubject.load(**kwargs)
    if old['spec'] != new['spec']:
        async with anarchy_subject.lock:
            await anarchy_subject.handle_update(previous_state=old)

@kopf.on.event(Anarchy.domain, Anarchy.version, 'anarchysubjects')
async def subject_event(event, **_):
    obj = event.get('object')
    if not obj or obj.get('apiVersion') != Anarchy.api_version:
        return

    metadata = obj['metadata']
    name = metadata['name']
    finalizers = metadata.get('finalizers', [])

    if event['type'] == 'DELETED':
        logging.info(f"Remove deleted AnarchySubject {name} from cache")
        AnarchySubject.cache.pop(name, None)
    elif Anarchy.domain in finalizers:
        # Subject is still handled through kopf framework
        pass
    elif Anarchy.subject_label in finalizers:
        # Kopf managed finalizer has been removed but subject label remains,
        # This should only happen during delete handling.
        if 'deletionTimestamp' in metadata:
            anarchy_subject = AnarchySubject.load_definition(obj)
            async with anarchy_subject.lock:
                await anarchy_subject.handle_post_delete_event()
        else:
            logging.error(
                f"AnarchySubject {name} does not have {Anarchy.domain} label "
                f"but does have {Anarchy.subject_label} but is not deleting?"
            )
    else:
        logging.debug(f"Ignoring deleting AnarchySubject {name}")


@kopf.on.create(Anarchy.domain, Anarchy.version, 'anarchyactions')
async def action_create(**kwargs):
    anarchy_action = AnarchyAction.load(**kwargs)
    anarchy_subject = await anarchy_action.get_subject()
    if not anarchy_subject:
        await anarchy_action.raise_error_if_still_exists(
            f"{anarchy_action} references missing AnarchySubject {anarchy_action.subject_name}",
        )
        return

    async with anarchy_subject.lock:
        await anarchy_action.handle_create(anarchy_subject)

@kopf.on.delete(Anarchy.domain, Anarchy.version, 'anarchyactions')
async def action_delete(**kwargs):
    anarchy_action = AnarchyAction.load(**kwargs)
    anarchy_action.remove_from_cache()

    anarchy_subject = await anarchy_action.get_subject()
    if not anarchy_subject:
        return

    async with anarchy_subject.lock:
        await anarchy_action.handle_delete(anarchy_subject)

@kopf.on.resume(Anarchy.domain, Anarchy.version, 'anarchyactions')
async def action_resume(**kwargs):
    anarchy_action = AnarchyAction.load(**kwargs)
    anarchy_subject = await anarchy_action.get_subject()
    if not anarchy_subject:
        await anarchy_action.raise_error_if_still_exists(
            f"{anarchy_action} references missing AnarchySubject {anarchy_action.subject_name}",
        )
        return

    async with anarchy_subject.lock:
        await anarchy_action.handle_resume(anarchy_subject)

@kopf.on.update(Anarchy.domain, Anarchy.version, 'anarchyactions')
async def action_update(**kwargs):
    anarchy_action = AnarchyAction.load(**kwargs)
    anarchy_subject = await anarchy_action.get_subject()
    if not anarchy_subject:
        if anarchy_action.is_delete_handler:
            return
        await anarchy_action.raise_error_if_still_exists(
            f"{anarchy_action} references missing AnarchySubject {anarchy_action.subject_name}",
        )
        return

    async with anarchy_subject.lock:
        await anarchy_action.handle_update(anarchy_subject)

@kopf.daemon(Anarchy.domain, Anarchy.version, 'anarchyactions', cancellation_timeout=1)
async def action_daemon(stopped, **kwargs):
    anarchy_action = AnarchyAction.load(**kwargs)
    anarchy_subject = await anarchy_action.get_subject()
    if not anarchy_subject:
        await anarchy_action.raise_error_if_still_exists(
            f"{anarchy_action} references missing AnarchySubject {anarchy_action.subject_name}",
        )
        return

    try:
        while not stopped:
            async with anarchy_subject.lock:
                sleep_interval = await anarchy_action.manage(anarchy_subject)
            if sleep_interval:
                await asyncio.sleep(sleep_interval)
    except asyncio.CancelledError:
        pass


@kopf.on.delete(Anarchy.domain, Anarchy.version, 'anarchyruns')
async def run_delete(**kwargs):
    anarchy_run = AnarchyRun.load(**kwargs)
    anarchy_run.remove_from_cache()

    anarchy_subject = await anarchy_run.get_subject()
    if not anarchy_subject:
        return

    if anarchy_run.has_action:
        anarchy_action = await anarchy_run.get_action()
    else:
        anarchy_action = None

    async with anarchy_subject.lock:
        await anarchy_run.handle_delete(anarchy_subject, anarchy_action)

@kopf.on.resume(Anarchy.domain, Anarchy.version, 'anarchyruns')
async def run_resume(**kwargs):
    anarchy_run = AnarchyRun.load(**kwargs)
    anarchy_subject = await anarchy_run.get_subject()
    if not anarchy_subject:
        await anarchy_run.raise_error_if_still_exists(
            f"{anarchy_run} references missing AnarchySubject {anarchy_run.subject_name}",
        )
        return

    if anarchy_run.has_action:
        anarchy_action = await anarchy_run.get_action()
        if not anarchy_action:
            await anarchy_run.raise_error_if_still_exists(
                f"{anarchy_run} references missing AnarchyAction {anarchy_run.action_name}",
            )
            return
    else:
        anarchy_action = None

    async with anarchy_subject.lock:
        await anarchy_run.handle_resume(anarchy_subject, anarchy_action)

@kopf.on.update(Anarchy.domain, Anarchy.version, 'anarchyruns')
async def run_update(new, old, **kwargs):
    anarchy_run = AnarchyRun.load(**kwargs)
    anarchy_subject = await anarchy_run.get_subject()
    if not anarchy_subject:
        if anarchy_run.is_delete_handler:
            return
        await anarchy_run.raise_error_if_still_exists(
            f"{anarchy_run} references missing AnarchySubject {anarchy_run.subject_name}",
        )
        return

    if anarchy_run.has_action:
        anarchy_action = await anarchy_run.get_action()
        if not anarchy_action:
            if anarchy_run.is_delete_handler:
                return
            await anarchy_run.raise_error_if_still_exists(
                f"{anarchy_run} references missing AnarchyAction {anarchy_run.action_name}",
            )
            return
    else:
        anarchy_action = None

    new_runner_state = new['metadata'].get('labels', {}).get(Anarchy.runner_label)
    old_runner_state = old['metadata'].get('labels', {}).get(Anarchy.runner_label)
    async with anarchy_subject.lock:
        if new_runner_state != old_runner_state:
            if new_runner_state == 'canceled':
                await anarchy_run.handle_canceled(anarchy_subject, anarchy_action)
            elif new_runner_state == 'failed':
                await anarchy_run.handle_failed(anarchy_subject, anarchy_action)
            elif new_runner_state == 'lost':
                await anarchy_run.handle_lost(anarchy_subject, anarchy_action)
            elif new_runner_state == 'pending':
                await anarchy_run.handle_pending(anarchy_subject, anarchy_action)
            elif new_runner_state == 'queued':
                logging.error(f"{anarchy_run} returned to state queued?")
            elif new_runner_state == 'successful':
                await anarchy_run.handle_success(anarchy_subject, anarchy_action)
            else:
                await anarchy_run.handle_running(anarchy_subject, anarchy_action)

@kopf.daemon(Anarchy.domain, Anarchy.version, 'anarchyruns', cancellation_timeout=1)
async def run_daemon(stopped, **kwargs):
    anarchy_run = AnarchyRun.load(**kwargs)
    anarchy_subject = await anarchy_run.get_subject()
    if not anarchy_subject:
        await anarchy_run.raise_error_if_still_exists(
            f"{anarchy_run} references missing AnarchySubject {anarchy_run.subject_name}",
        )
        return

    if anarchy_run.has_action:
        anarchy_action = await anarchy_run.get_action()
        if not anarchy_action:
            await anarchy_run.raise_error_if_still_exists(
                f"{anarchy_run} references missing AnarchyAction {anarchy_run.action_name}",
            )
            return
    else:
        anarchy_action = None

    try:
        while not stopped:
            async with anarchy_subject.lock:
                sleep_interval = await anarchy_run.manage(anarchy_subject, anarchy_action)
            if sleep_interval:
                await asyncio.sleep(sleep_interval)
    except asyncio.CancelledError:
        pass

if not Anarchy.running_all_in_one:
    @kopf.on.create(Anarchy.domain, Anarchy.version, 'anarchyrunners')
    async def runner_create(**kwargs):
        anarchy_runner = AnarchyRunner.load(logger, **kwargs)
        async with anarchy_runner.lock:
            await anarchy_runner.handle_create(logger=logger)

    @kopf.on.delete(Anarchy.domain, Anarchy.version, 'anarchyrunners')
    async def runner_delete(logger, **kwargs):
        anarchy_runner = AnarchyRunner.load(**kwargs)
        async with anarchy_runner.lock:
            await anarchy_runner.handle_delete(logger=logger)

    @kopf.on.resume(Anarchy.domain, Anarchy.version, 'anarchyrunners')
    async def runner_resume(logger, **kwargs):
        anarchy_runner = AnarchyRunner.load(**kwargs)
        async with anarchy_runner.lock:
            await anarchy_runner.handle_resume(logger=logger)

    @kopf.on.update(Anarchy.domain, Anarchy.version, 'anarchyrunners')
    async def runner_update(logger, **kwargs):
        anarchy_runner = AnarchyRunner.load(**kwargs)
        async with anarchy_runner.lock:
            await anarchy_runner.handle_update(logger=logger)

    @kopf.daemon(Anarchy.domain, Anarchy.version, 'anarchyrunners', cancellation_timeout=1)
    async def runner_daemon(logger, stopped, **kwargs):
        anarchy_runner = AnarchyRunner.load(**kwargs)
        try:
            while not stopped:
                async with anarchy_runner.lock:
                    await anarchy_runner.manage_pods(logger=logger)
                await asyncio.sleep(anarchy_runner.scaling_check_interval)
        except asyncio.CancelledError:
            pass

    @kopf.on.event('pods', labels={Anarchy.runner_label: kopf.PRESENT})
    async def runner_pod_event(event, logger, **_):
        obj = event.get('object')
        if not obj or obj.get('kind') != 'Pod':
            logging.warning(f"Weird event {event}")
            return

        pod = Anarchy.k8s_obj_from_dict(obj, kubernetes_asyncio.client.V1Pod)
        anarchy_runner_name = pod.metadata.labels[Anarchy.runner_label]
        anarchy_runner = await AnarchyRunner.get(anarchy_runner_name)
        if not anarchy_runner:
            logger.warning(f"AnarchyRunner {anarchy_runner_name} not found for Pod {pod.metadata.name}")
            return
        async with anarchy_runner.lock:
            if event['type'] == 'DELETED':
                await anarchy_runner.handle_runner_pod_deleted(pod=pod, logger=logger)
            else:
                await anarchy_runner.handle_runner_pod_event(pod=pod, logger=logger)
