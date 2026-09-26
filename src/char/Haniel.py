from src.char.Support import Support
from src.combat.planner import CombatContext, Planner

SKILL_SHORT_TIMEOUT = 2.0


class Haniel(Support):
    """Haniel - BLUE support.

    SUB_DPS, SETUP_ONLY: Q to deploy the enhanced domain, then E to deploy the
    companion, then leave the field. Self-contained and independent of any specific
    team composition; higher-level coordination is handled outside this file.
    """

    cn_name = "哈妮娅"
    element = Support.ElementType.BLUE

    def combat_plan(self, context: CombatContext):
        ultimate = self.click_ultimate_action(add_tags=Planner.ActionTag.TEAM_BUFF)
        skill = self.click_skill_action()

        def entry():
            if (yield ultimate):
                self.sleep(0.3)
            if (yield skill):
                self.sleep(0.3)

        return self.plan(ultimate, skill, entry=entry)
