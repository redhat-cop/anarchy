#!/usr/bin/env python

import unittest
import sys
sys.path.append('../operator')

import kubernetes_asyncio

from datetime import datetime, timedelta, timezone
from anarchyrunner import AnarchyRunner

class TestAnarchyRunner(unittest.TestCase):
    def setUp(self):
        self.anarchyrunner = AnarchyRunner(
            annotations = {},
            labels = {},
            meta = {},
            name = "test",
            namespace = "anarchy",
            spec = {},
            status = {},
            uid = '00000000-0000-0000-0000-000000000000'
        )
        self.anarchyrunner.pods = {}
        self.anarchyrunner.pods_preloaded = True

    def test_max_replicas_reached(self):
        self.anarchyrunner.spec = {
            "maxReplicas": 2,
        }
        self.anarchyrunner.pods = {
            "anarchy-runner-0": kubernetes_asyncio.client.V1Pod({}),
        }
        self.assertFalse(
            self.anarchyrunner.max_replicas_reached,
            msg="max_replicas_reached when should be under max_replicas",
        )

        self.anarchyrunner.pods = {
            "anarchy-runner-0": kubernetes_asyncio.client.V1Pod({}),
            "anarchy-runner-1": kubernetes_asyncio.client.V1Pod({}),
        }
        self.assertTrue(
            self.anarchyrunner.max_replicas_reached,
            msg="max_replicas_reached false when at max_replicas",
        )

    def test_scale_up_delay(self):
        self.anarchyrunner.spec = {
            "scaleUpDelay": "5m"
        }
        self.anarchyrunner.last_scale_up_datetime = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.assertFalse(
            self.anarchyrunner.scale_up_delay_exceeded,
            msg="scale_up_delay_exceeded when configured for 5m and 1m passed",
        )

        self.anarchyrunner.last_scale_up_datetime = datetime.now(timezone.utc) - timedelta(minutes=10)
        self.assertTrue(
            self.anarchyrunner.scale_up_delay_exceeded,
            msg="scale_up_delay_exceeded false when configured for 5m and 10m passed",
        )


    def test_scale_up_threshold_exceeded(self):
        self.anarchyrunner.spec = {
            "scaleUpThreshold": 3,
        }
        self.anarchyrunner.status = {
            "pendingRuns": [],
            "pods": [{
                "name": "anarchy-runner-0",
            }]
        }
        self.assertFalse(
            self.anarchyrunner.scale_up_threshold_exceeded,
            msg="scale_up_threshold_exceeded with no pendingRuns"
        )

        self.anarchyrunner.status["pendingRuns"] = [
            {"name": "anarchy-run-0"},
            {"name": "anarchy-run-1"},
            {"name": "anarchy-run-2"},
            {"name": "anarchy-run-3"},
        ]
        self.assertFalse(
            self.anarchyrunner.scale_up_threshold_exceeded,
            msg="scale_up_threshold exceeded with idle runner pod"
        )

        self.anarchyrunner.status['pods'][0]['run'] = {"name": "anarchy-run-acive"}
        self.assertTrue(
            self.anarchyrunner.scale_up_threshold_exceeded,
            msg="scale_up_threshold not exceeded with active pod"
        )


    def test_scaling_enabled(self):
        self.assertFalse(
            self.anarchyrunner.scaling_enabled,
            msg="scaling_enabled when scaleUpThreshold is not set",
        )
        self.anarchyrunner.spec = {
            "scaleUpThreshold": 5,
        }
        self.assertTrue(
            self.anarchyrunner.scaling_enabled,
            msg="scaling_enabled false when scaleUpThreshold is set",
        )


if __name__ == '__main__':
    unittest.main()
