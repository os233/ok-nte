import time

from ok import TaskDisabledException, WaitFailedException

from src.tasks.AutoBidAuctionTask import RE_MAIN_ASSET_TITLE, AutoBidAuctionTask
from src.tasks.NTEOneTimeTask import NTEOneTimeTask


class AuctionHouseUpkeepTask(AutoBidAuctionTask):
    """拍卖行维护: 在拍卖主界面出售藏品或领取低保金, 不进行竞拍。

    复用自动拍卖任务的拍卖行 UI 区域与藏品出售, 低保金流程, 仅收缩配置面,
    让维护动作可以脱离竞拍单独执行。
    """

    # --- 维护配置 ---
    CONF_SELL_COLLECTIONS = "出售藏品"
    CONF_CLAIM_WELFARE = "领取低保金"

    # --- 超时 (秒) ---
    UPKEEP_TIMEOUT = 120
    MAIN_SCREEN_TIMEOUT = 15

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "自动拍卖维护"
        self.description = "在拍卖主界面使用, 出售藏品或领取低保金, 不进行竞拍"
        self.group_name = "都市闲趣"

        # 隐藏继承自自动拍卖的竞拍配置, 只保留维护相关配置.
        self.default_config.clear()
        self.default_config.update(
            {
                self.CONF_SELL_COLLECTIONS: True,
                self.CONF_CLAIM_WELFARE: True,
                self.CONF_KEEP_QUALITIES: ["品质红"],
            }
        )

        self.config_type = {
            self.CONF_KEEP_QUALITIES: {
                "type": "multi_selection",
                "options": list(self.QUALITY_KEYS),
            }
        }

        self.config_description = {
            self.CONF_SELL_COLLECTIONS: "打开藏品仓库并按品质出售",
            self.CONF_CLAIM_WELFARE: "我的资产低于10万时领取",
            self.CONF_KEEP_QUALITIES: "勾选品质不会被出售",
        }
        self.add_exit_after_config()

    # --- 任务入口 ---
    def run(self):
        """任务入口, 复用一次性任务的前置检查。"""
        # 必须显式调用 NTEOneTimeTask.run: super().run() 会解析到
        # AutoBidAuctionTask.run, 从而执行完整竞拍流程.
        NTEOneTimeTask.run(self)
        try:
            self.do_upkeep()
        except TaskDisabledException:
            raise
        except Exception as e:
            self.log_error("拍卖行维护任务执行异常", e)
            raise

    def do_upkeep(self):
        """执行维护流程: 等待拍卖主界面, 然后按配置领取低保金和出售藏品。"""
        sell_enabled = self.config.get(self.CONF_SELL_COLLECTIONS, True)
        welfare_enabled = self.config.get(self.CONF_CLAIM_WELFARE, True)
        if not sell_enabled and not welfare_enabled:
            self.log_warning("出售藏品与领取低保金均未启用, 本次任务不执行任何操作")
            return

        deadline = time.monotonic() + self.UPKEEP_TIMEOUT
        boxes = self._build_boxes()

        self.info_set("当前阶段", "等待主界面")
        if not self._wait_auction_main_screen(boxes):
            raise WaitFailedException("未检测到拍卖主界面, 请先打开拍卖行")

        if welfare_enabled:
            self.info_set("当前阶段", "领取低保金")
            self._claim_welfare_if_needed(boxes, deadline)

        if sell_enabled:
            self.info_set("当前阶段", "出售藏品")
            self._sell_collections(boxes, deadline)

        self.info_set("当前阶段", "完成")
        self.log_info("拍卖行维护完成")

    def _wait_auction_main_screen(self, boxes) -> bool:
        """等待拍卖主界面加载完成, 以"我的资产"标题为标志。"""
        title = self.wait_ocr(
            box=boxes.main_asset_title,
            match=RE_MAIN_ASSET_TITLE,
            time_out=self.MAIN_SCREEN_TIMEOUT,
            raise_if_not_found=False,
            settle_time=0.5,
            post_action=lambda: self.sleep(0.5),
        )
        if not title:
            self.log_warning("拍卖主界面加载标志未识别")
            return False

        self.log_info("已进入拍卖主界面")
        return True
