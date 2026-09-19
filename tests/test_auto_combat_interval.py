import unittest

from src.tasks.trigger.AutoCombatTask import AutoCombatTask


class AutoCombatIdleIntervalTests(unittest.TestCase):
    def test_valid_values_pass_through(self):
        self.assertEqual(AutoCombatTask._clip_interval(0.5), 0.5)
        self.assertEqual(AutoCombatTask._clip_interval("2"), 2.0)

    def test_zero_is_clamped_positive(self):
        # trigger_interval 为 0 时框架会每个周期都触发, 必须夹取为正值
        self.assertGreater(AutoCombatTask._clip_interval(0), 0)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(AutoCombatTask._clip_interval(-1), AutoCombatTask.MIN_IDLE_INTERVAL)
        self.assertEqual(AutoCombatTask._clip_interval(100), AutoCombatTask.MAX_IDLE_INTERVAL)

    def test_invalid_values_fall_back_to_default(self):
        for value in (None, "", "abc", [0.2]):
            with self.subTest(value=value):
                self.assertEqual(
                    AutoCombatTask._clip_interval(value), AutoCombatTask.DEFAULT_IDLE_INTERVAL
                )

    def test_default_config_declares_interval(self):
        defaults = AutoCombatTask.__dict__.get("DEFAULT_IDLE_INTERVAL")
        self.assertEqual(defaults, 0.1)


if __name__ == "__main__":
    unittest.main()
