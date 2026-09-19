import time

from ok import Logger, TriggerTask

from src.combat.BaseCombatTask import BaseCombatTask, NotInCombatException

logger = Logger.get_logger(__name__)


class AutoCombatTask(BaseCombatTask, TriggerTask):
    CONF_USE_ULT = "使用终结技"
    CONF_AUTO_TARGET = "自动目标"
    CONF_IDLE_INTERVAL = "待机检查间隔"
    DEFAULT_IDLE_INTERVAL = 0.1
    MIN_IDLE_INTERVAL = 0.05
    MAX_IDLE_INTERVAL = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.default_config = {"_enabled": True}
        self.trigger_interval = self.DEFAULT_IDLE_INTERVAL
        self.name = "自动战斗"
        self.description = "受《异环》UI的特殊性影响, 部分场景下存在识别稳定性波动"
        self.last_is_click = False
        self.default_config.update(
            {
                self.CONF_AUTO_TARGET: True,
                self.CONF_USE_ULT: True,
                self.CONF_IDLE_INTERVAL: self.DEFAULT_IDLE_INTERVAL,
            }
        )
        self.config_description = {
            self.CONF_AUTO_TARGET: "关闭时仅在中键选中敌人且画面识别到 'Lv' 文字时开启战斗",
            self.CONF_IDLE_INTERVAL: (
                "未进入战斗时的检查间隔(秒), 调大可降低待机时的 CPU 占用"
            ),
        }

    @classmethod
    def _clip_interval(cls, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return cls.DEFAULT_IDLE_INTERVAL
        return min(cls.MAX_IDLE_INTERVAL, max(cls.MIN_IDLE_INTERVAL, value))

    def run(self):
        # 框架每次调度都会重新读取 trigger_interval, 这里按配置即时生效;
        # 不能为 0, 否则触发间隔归零会退化为每个 executor 周期都截屏
        self.trigger_interval = self._clip_interval(self.config.get(self.CONF_IDLE_INTERVAL))
        if not self.scene.is_in_team(self.is_in_team):
            return

        if not self.in_combat():
            return

        try:
            self.combat_session.use_ultimate = self.config.get(self.CONF_USE_ULT, True)
            self.begin_combat_session()
            while self.in_combat():
                self.get_current_char(raise_exception=True).perform()
        except NotInCombatException as e:
            logger.info(f"Out of combat {int(time.time() - self.combat_session.combat_start)} {e}")
        finally:
            self.combat_end()
