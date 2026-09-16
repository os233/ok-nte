import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.heist_path.HeistPath import HeistPath
from src.runtime import services
from src.sound_trigger.SoundCombatContext import SoundCombatContext


class FakeListener:
    def __init__(self):
        self.threshold = None
        self.counter_attack_threshold = None
        self.running = False
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def is_running(self):
        return self.running

    def start(self):
        self.start_calls += 1
        self.running = True
        return True

    def stop(self):
        self.stop_calls += 1
        self.running = False


class SoundContextListenerLifecycleTests(unittest.TestCase):
    def setUp(self):
        SoundCombatContext._instance = None

    def tearDown(self):
        SoundCombatContext._instance = None

    def _context_with_listener(self):
        context = SoundCombatContext()
        context._is_active = True
        listener = FakeListener()
        context._listener = listener
        return context, listener

    def test_update_config_starts_listener_once_when_enabled(self):
        context, listener = self._context_with_listener()
        context.update_config(True, True, 0.2, 0.15)
        context.update_config(True, False, 0.3, 0.2)
        self.assertEqual(listener.start_calls, 1)
        self.assertTrue(listener.is_running)
        self.assertEqual(listener.threshold, 0.3)
        self.assertEqual(listener.counter_attack_threshold, 0.2)

    def test_update_config_stops_listener_when_disabled(self):
        context, listener = self._context_with_listener()
        listener.running = True
        context.update_config(False, True, 0.2, 0.15)
        self.assertEqual(listener.stop_calls, 1)
        self.assertFalse(listener.is_running)
        self.assertFalse(context._enable_sound_trigger)
        self.assertEqual(context._pending_config, (False, True, 0.2, 0.15))
        context.update_config(False, True, 0.2, 0.15)
        self.assertEqual(listener.stop_calls, 1)

    def test_update_config_restarts_listener_after_reenable(self):
        context, listener = self._context_with_listener()
        listener.running = True
        context.update_config(False, True, 0.2, 0.15)
        context.update_config(True, True, 0.2, 0.15)
        self.assertEqual(listener.start_calls, 1)
        self.assertTrue(listener.is_running)

    def test_update_config_without_listener_only_stores_config(self):
        context = SoundCombatContext()
        context.update_config(True, True, 0.2, 0.15)
        self.assertTrue(context._enable_sound_trigger)
        self.assertEqual(context._pending_config, (True, True, 0.2, 0.15))


class FakeStartupContext:
    def __init__(self):
        self.setup_calls = []
        self.enter_calls = 0
        self.shutdown_calls = 0

    def setup(self, **kwargs):
        self.setup_calls.append(kwargs)

    def enter(self):
        self.enter_calls += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1


class FakeGlobalConfig:
    def __init__(self, enabled):
        self.enabled = enabled

    def get_config(self, name):
        return {"Enable Sound Trigger": self.enabled}


class BrokenGlobalConfig:
    def get_config(self, name):
        raise RuntimeError("config not ready")


class RuntimeServicesSoundGateTests(unittest.TestCase):
    def _init_sound_context(self, global_config):
        fake_context = FakeStartupContext()
        with (
            patch(
                "src.sound_trigger.SoundCombatContext.SoundCombatContext",
                lambda: fake_context,
            ),
            patch.object(services, "og", SimpleNamespace(global_config=global_config)),
            patch.object(
                services, "get_path_relative_to_exe", lambda *parts: "/".join(parts)
            ),
        ):
            services.RuntimeServices()._init_sound_context()
        return fake_context

    def test_disabled_config_keeps_context_but_skips_listener(self):
        fake_context = self._init_sound_context(FakeGlobalConfig(False))
        self.assertEqual(len(fake_context.setup_calls), 1)
        self.assertEqual(fake_context.enter_calls, 0)
        self.assertEqual(fake_context.shutdown_calls, 0)

    def test_enabled_config_enters_listener(self):
        fake_context = self._init_sound_context(FakeGlobalConfig(True))
        self.assertEqual(len(fake_context.setup_calls), 1)
        self.assertEqual(fake_context.enter_calls, 1)
        self.assertEqual(fake_context.shutdown_calls, 0)

    def test_unreadable_config_defaults_to_enabled(self):
        fake_context = self._init_sound_context(BrokenGlobalConfig())
        self.assertEqual(fake_context.enter_calls, 1)

    def test_missing_config_defaults_to_enabled(self):
        fake_context = self._init_sound_context(None)
        self.assertEqual(fake_context.enter_calls, 1)


class FakeHeistTask:
    def __init__(self):
        self.sleep_calls = []

    def sleep(self, timeout):
        self.sleep_calls.append(timeout)
        time.sleep(timeout)


class HeistPathSleepTests(unittest.TestCase):
    def test_long_sleep_delegates_bulk_to_task_sleep(self):
        task = FakeHeistTask()
        path = HeistPath(task)
        start = time.perf_counter()
        path.sleep(0.06)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(task.sleep_calls), 1)
        self.assertAlmostEqual(task.sleep_calls[0], 0.04, places=6)
        self.assertGreaterEqual(elapsed, 0.055)
        self.assertLess(elapsed, 0.2)

    def test_short_sleep_uses_time_sleep_instead_of_spinning(self):
        task = FakeHeistTask()
        path = HeistPath(task)
        real_sleep = time.sleep
        tail_sleeps = []

        def counting_sleep(duration):
            tail_sleeps.append(duration)
            real_sleep(duration)

        start = time.perf_counter()
        with patch("src.heist_path.HeistPath.time.sleep", side_effect=counting_sleep):
            path.sleep(0.012)
        elapsed = time.perf_counter() - start
        self.assertEqual(task.sleep_calls, [])
        self.assertTrue(any(duration > 0 for duration in tail_sleeps))
        self.assertGreaterEqual(elapsed, 0.011)
        self.assertLess(elapsed, 0.15)


if __name__ == "__main__":
    unittest.main()
