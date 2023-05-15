#!/usr/bin/env python

import logging
import kopf

from configure_kopf_logging import configure_kopf_logging
from infinite_relative_backoff import InfiniteRelativeBackoff
from anarchycommune import AnarchyCommune
from anarchy import Anarchy

@kopf.on.startup()
async def on_startup(settings: kopf.OperatorSettings, **_):
    await Anarchy.on_startup()

    # Never give up from network errors
    settings.networking.error_backoffs = InfiniteRelativeBackoff()

    # Store last handled configuration in status
    settings.persistence.diffbase_storage = kopf.StatusDiffBaseStorage(field='status.diffBase')

    # Use operator domain as finalizer
    settings.persistence.finalizer = Anarchy.domain

    # Store progress in status.
    settings.persistence.progress_storage = kopf.StatusProgressStorage(field='status.kopf.progress')

    # Only create events for warnings and errors
    settings.posting.level = logging.WARNING

    # Disable scanning for crds and namespaces
    settings.scanning.disabled = True

    configure_kopf_logging()

@kopf.on.cleanup()
async def on_cleanup(**_):
    await Anarchy.on_cleanup()

@kopf.on.create(Anarchy.domain, Anarchy.version, 'anarchycommunes')
async def commune_create(logger, **kwargs):
    anarchy_commune = AnarchyCommune.load(**kwargs)
    async with anarchy_commune.lock:
        await anarchy_commune.handle_create(logger=logger)

@kopf.on.resume(Anarchy.domain, Anarchy.version, 'anarchycommunes')
async def commune_resume(logger, **kwargs):
    anarchy_commune = AnarchyCommune.load(**kwargs)
    async with anarchy_commune.lock:
        await anarchy_commune.handle_resume(logger=logger)

@kopf.on.update(Anarchy.domain, Anarchy.version, 'anarchycommunes')
async def commune_update(logger, **kwargs):
    anarchy_commune = AnarchyCommune.load(**kwargs)
    async with anarchy_commune.lock:
        await anarchy_commune.handle_update(logger=logger)

@kopf.on.delete(Anarchy.domain, Anarchy.version, 'anarchycommunes')
async def commune_delete(logger, **kwargs):
    anarchy_commune = AnarchyCommune.load(**kwargs)
    async with anarchy_commune.lock:
        await anarchy_commune.handle_delete(logger=logger)
