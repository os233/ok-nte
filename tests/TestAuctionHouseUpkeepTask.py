import unittest
from unittest.mock import patch

from ok import WaitFailedException

from src.tasks.AuctionHouseUpkeepTask import AuctionHouseUpkeepTask
from src.tasks.NTEOneTimeTask import NTEOneTimeTask


class TestAuctionHouseUpkeepTask(unittest.TestCase):
    """覆盖拍卖行维护任务的配置分流逻辑, 不依赖游戏窗口。"""

    def _make_task(self, main_screen=True, **config):
        task = object.__new__(AuctionHouseUpkeepTask)
        task.config = {
            AuctionHouseUpkeepTask.CONF_SELL_COLLECTIONS: True,
            AuctionHouseUpkeepTask.CONF_CLAIM_WELFARE: True,
        }
        task.config.update(config)
        task.logs = []
        task.calls = []
        task.log_info = lambda message, **kwargs: task.logs.append(("info", message))
        task.log_warning = lambda message, **kwargs: task.logs.append(("warning", message))
        task.log_debug = lambda message, **kwargs: task.logs.append(("debug", message))
        task.log_error = lambda message, error=None, **kwargs: task.logs.append(("error", message))
        task.info_set = lambda key, value: None
        task._build_boxes = lambda: object()
        task._wait_auction_main_screen = lambda boxes: main_screen
        task._claim_welfare_if_needed = lambda boxes, deadline: task.calls.append("welfare")
        task._sell_collections = lambda boxes, deadline=None: task.calls.append("sell")
        return task

    def test_runs_welfare_before_sell_by_default(self):
        task = self._make_task()

        task.do_upkeep()

        self.assertEqual(task.calls, ["welfare", "sell"])

    def test_sell_only_skips_welfare(self):
        task = self._make_task(**{AuctionHouseUpkeepTask.CONF_CLAIM_WELFARE: False})

        task.do_upkeep()

        self.assertEqual(task.calls, ["sell"])

    def test_welfare_only_skips_sell(self):
        task = self._make_task(**{AuctionHouseUpkeepTask.CONF_SELL_COLLECTIONS: False})

        task.do_upkeep()

        self.assertEqual(task.calls, ["welfare"])

    def test_all_disabled_does_not_touch_game(self):
        task = self._make_task(
            main_screen=False,
            **{
                AuctionHouseUpkeepTask.CONF_SELL_COLLECTIONS: False,
                AuctionHouseUpkeepTask.CONF_CLAIM_WELFARE: False,
            },
        )

        task.do_upkeep()

        self.assertEqual(task.calls, [])

    def test_missing_main_screen_aborts_without_actions(self):
        task = self._make_task(main_screen=False)

        with self.assertRaises(WaitFailedException):
            task.do_upkeep()

        self.assertEqual(task.calls, [])

    def test_run_does_not_enter_auction_flow(self):
        """run() 不得命中 AutoBidAuctionTask.run, 否则会执行完整竞拍流程。"""
        task = self._make_task()
        task.do_run = lambda: task.calls.append("auction")
        task.do_upkeep = lambda: task.calls.append("upkeep")

        with patch.object(NTEOneTimeTask, "run", lambda self, *args, **kwargs: None):
            task.run()

        self.assertEqual(task.calls, ["upkeep"])


if __name__ == "__main__":
    unittest.main()
