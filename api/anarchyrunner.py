import logging

from anarchy import Anarchy
from anarchywatchobject import AnarchyWatchObject

import anarchyrun
import anarchyrunnerpod

class AnarchyRunner(AnarchyWatchObject):
    cache = {}
    kind = 'AnarchyRunner'
    plural = 'anarchyrunners'
    preload = True

    @classmethod
    async def on_startup(cls):
        if Anarchy.running_all_in_one:
            await cls.on_startup_running_all_in_one()
        else:
            await super().on_startup()

    @classmethod
    async def on_startup_running_all_in_one(cls):
        default = cls({
            "apiVersion": Anarchy.api_version,
            "kind": "AnarchyRunner",
            "metadata": {
                "name": "default",
                "namespace": Anarchy.namespace,
                "resourceVersion": 0,
                "uid": "00000000-0000-0000-0000-000000000000",
            },
            "spec": {
                "maxReplicas": 1,
                "minReplicas": 1,
            }
        })
        await cls.cache_put(default)
        logging.info(f"Cache preloaded {default}")

    async def update_status(self):
        if Anarchy.running_all_in_one:
            return
        pods_status = []
        for pod in anarchyrunnerpod.AnarchyRunnerPod.cache.values():
            run = anarchyrun.AnarchyRun.get_run_assigned_to_runner_pod(pod)
            pods_status.append({
                "consecutiveFailureCount": pod.consecutive_failure_count,
                "name": pod.name,
                "run": run.as_reference() if run else None,
                "runCount": pod.run_count,
            })
        await self.merge_patch_status({
            "pendingRuns": [
                {
                    "name": name,
                } for name in anarchyrun.AnarchyRun.pending_run_names
            ],
            "pods": pods_status,
        })
