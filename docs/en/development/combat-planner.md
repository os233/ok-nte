# Combat Planner Development Guide

> **Tip**: Concrete character implementations can be found in [`src/char`](../../../src/char), or viewed in the
> [GitHub](https://github.com/BnanZ0/ok-nte/tree/main/src/char) and
> [CNB](https://cnb.cool/BnanZ0/ok-nte-update/-/tree/main/src/char) code directories.

The planner is the team's brain. A character declares one `CombatPlan`:

- `actions`: the action catalog visible to the planner, used for switch scoring and route/request/reservation matching.
- `claims`: `FieldClaim` entry requests that express "I should be switched in now".
- `entry`: the Python generator action flow for an ordinary entry. When omitted, actions run in declaration order.

The public import entry point is fixed:

```python
from src.combat.planner import ActionSlot, CombatContext, FieldClaim, Planner, RoleProfile
```

`src.combat.planner` exports only the official development API. Character code must not import internal modules such as `planner/core.py`, `planner/requests.py`, or `planner/state.py` directly.

## Quick Entry

An ordinary character usually only needs to override `describe_role()` and `combat_plan(context)`:

```python
def describe_role(self):
    return RoleProfile(
        role=Planner.Role.SUB_DPS,
        field_preference=Planner.FieldPreference.SUB_DPS,
        max_field_time=1.5,
    )

def combat_plan(self, context: CombatContext):
    return self.plan(
        self.click_ultimate_action(),
        self.click_skill_action(),
    )
```

For complex action ordering, define entry flow with action variables in the same plan. An action can run only once in one entry; use `repeat_for_entry()` for a limited extra execution:

```python
def combat_plan(self, context: CombatContext):
    skill = self.click_skill_action(reason="skill available")
    ultimate = self.click_ultimate_action(reason="ultimate available")

    def entry():
        skill_result = yield skill
        if skill_result and self.ultimate_available():
            self.sleep(0.6)

        ultimate_result = yield ultimate
        if ultimate_result:
            yield skill.repeat_for_entry()

    return self.plan(skill, ultimate, entry=entry)
```

`yield action` gives the action to the planner. The planner checks reservations and `can_execute`, executes the action, records the result, advances the request, and sends the `ActionResult` back to the generator. `bool(ActionResult)` equals `result.success`, so you can write:

```python
a = yield action_a
b = yield action_b

if a and b:
    yield action_c

if a and not b:
    yield fallback_action
```

## CombatPlan

Create a `CombatPlan` with `self.plan(*actions, claims=None, entry=None)`:

```python
def combat_plan(self, context):
    setup = self.planner_action(...)
    claims = []
    if self.should_claim_field():
        claims.append(FieldClaim.high(reason="burst window"))

    return self.plan(setup, claims=claims)
```

Rules:

- Only declare actions and entry requests when creating a plan; do not send input.
- Do not call `context.request_route()`, `reserve_actions()`, or `request_tags()` when creating a plan. Publish these one-time requests in action execution, or after receiving a successful result in the entry flow.
- `actions` is the catalog used for scoring and coordination matching; `entry` is the ordinary entry execution flow.
- Multiple independent `claims` may be passed. They do not stack scores; the planner uses only the highest-priority matching claim for the current character.
- Strict routes, expected entries, and active requests take scheduling priority over ordinary entry flow.
- An ordinary entry flow executes at most `MAX_ACTIONS_PER_ENTRY` actions.
- The same action is executed at most once during one entry.

## ActionIntent

`ActionIntent` expresses "what the character may try after entering the field". Do not split normal attacks, waits, or repeated presses into many actions; keep those details inside one action's `execute` function.

Fields:

- `tags: set[ActionTag]`: action meaning and scoring basis.
- `execute: Callable[[CombatContext], ActionResult | bool | None]`: the actual action.
- `name: str = ""`: advanced exact matching and log name.
- `slot: ActionSlot | None = None`: action slot. Coordination routes and reservations should preferably match by slot.
- `reason: str = ""`: planner log and switch reason.
- `can_execute: Callable[[CombatContext], bool] | None`: a hard restriction at planner level.
- `priority_ready: Callable[[CombatContext], bool] | None`: used only for switch scoring.

`action.repeat_for_entry()` returns an action copy that can be yielded again in the same entry. It preserves execution, slot, tags, and `can_execute` restrictions, and automatically creates an independent entry deduplication result for each call. It is suitable for a limited flow such as `Q -> E -> try E once more`; the copy should normally be yielded only in the entry flow and should not be added to `CombatPlan.actions`.

Every `yield` counts toward the action limit for one entry. Do not yield it inside a long-running loop; such loops should call `context.is_action_allowed(self, action)` before invoking an existing action helper. This keeps the loop under character-code control while still honoring planner `can_execute` and reservation rules.

When an action has a `slot`, the planner automatically checks reservations through `context.is_slot_available(...)`. A developer-provided `can_execute` only needs to express additional mechanic restrictions. To pre-check a complete action outside an entry flow, use `context.is_action_allowed(self, action)`; it checks both `can_execute` and slot reservations. Ordinary or limited entry actions should still be yielded directly.

`execute` return rules:

- Return `True`: success.
- Return `False` / `None` / no `return`: failure.
- Return `ActionResult`: use `ActionResult.success`.
- Truthy values such as `1` or `"ok"` are not treated as success.

Ordinary characters do not need to construct `ActionResult` manually. Create one only when a custom result name, tags, slot, or reason is required.

## ActionTag

`ActionTag` expresses action meaning and scoring. It must not express a mechanism belonging to one specific character.

Common tags:

- `ULTIMATE_ACTION`: Q.
- `SKILL_ACTION`: E.
- `ARC_ACTION`: Arc action, scored as 0.
- `SUPPORT`: Support, healing, or buff action.
- `TEAM_BUFF`: A key team-wide buff. Use only when the buff should be cast before the main DPS ultimate.
- `COORDINATION`: An action that publishes a coordination route or window.
- `COORDINATION_FINISHER`: A finishing action after coordination is complete.
- `FIELD_TIME`: A planner-built field-time action; characters should not declare it themselves.
- `LEGACY_COMBO`: Legacy combo action.
- `DEFAULT_ACTION`: Low-value fallback entry.

Switch scoring does not add all actions for the same character together; the planner uses the highest-scoring ready action as that character's representative. Tags do not control ordinary entry flow; `CombatPlan.entry` does.

## ActionSlot

`ActionSlot` is the action slot used for coordination matching and is preferred over action names.

Common slots:

- `SKILL`: E.
- `ULTIMATE`: Q.
- `ARC`: Arc.
- `ENTRY_REACTION`: Entry or ring reaction, not a key action.
- `FIELD_TIME`: Planner-built field time.
- `LEGACY_COMBO`: Legacy combo.
- `CUSTOM`: Special action.

Prefer writing coordination and reservations as:

```python
FollowupStep.for_action(zero, ActionSlot.SKILL)
ActionReservation.for_action(nanally, ActionSlot.SKILL)
context.is_slot_available(self, ActionSlot.SKILL)
```

## BaseChar Helpers

### Combat Session and First Engagement

`BaseCombatTask.begin_combat_session()` is the unified entry point when combat officially starts. It creates the public `task.combat_session`, makes the initial switch decision, and records the actual starting character. `CombatPlanner` only decides the initial switch target; it does not send input or manage the session. `CombatSession.combat_start` is the time combat began; `use_ultimate` and `switch_enabled` are fixed strategies for the current combat.

At the start of `BaseChar.perform()`, the first character to execute actual combat logic in the current battle is recorded. Character logic can use:

```python
if self.is_first_engage():
    # First character to actually engage in this battle
    ...

if self.consume_first_engage():
    # Returns True only once for the whole battle
    ...
```

`is_first_engage()` remains stable during the battle; `consume_first_engage()` returns `True` only once. Neither depends on initial-switch duration or a time window. `task.combat_session` creates a default session on first access. If a task must disable the initial switch and all later switches, set `task.combat_session.switch_enabled = False` before calling `begin_combat_session()`; do not replace the switching method at runtime.

### `click_ultimate_action`

```python
self.click_ultimate_action(
    name=None,
    tags=None,
    add_tags=None,
    reason="ultimate action available",
    can_execute=None,
)
```

- Automatically sets `slot=ActionSlot.ULTIMATE`.
- Defaults to `tags={ActionTag.ULTIMATE_ACTION}`.
- `tags` completely specifies the base tags; `add_tags` can receive one tag or a tag set and appends to them, or appends to the default tags when `tags` is omitted.
- Defaults to `name=f"{character_name}_ultimate"`.
- `can_execute` includes `self.ultimate_available()` by default; an extra condition is combined with it.
- `priority_ready` automatically uses `self.ultimate_available()`.
- `execute` calls `self.click_ultimate()`.

### `click_skill_action`

```python
self.click_skill_action(
    name=None,
    tags=None,
    add_tags=None,
    reason="skill action available",
    down_time=0.01,
    can_execute=None,
)
```

- Automatically sets `slot=ActionSlot.SKILL`.
- Defaults to `tags={ActionTag.SKILL_ACTION}`.
- `tags` completely specifies the base tags; `add_tags` can receive one tag or a tag set and appends to them, or appends to the default tags when `tags` is omitted.
- Defaults to `name=f"{character_name}_skill"`.
- `can_execute` includes `self.skill_available()` by default; an extra condition is combined with it.
- `priority_ready` automatically uses `self.skill_available()`.
- `execute` calls `self.click_skill(down_time=down_time)`.

### `planner_action`

```python
self.planner_action(
    tags={ActionTag.SKILL_ACTION},
    execute=self.some_action,
    name=None,
    slot=None,
    reason="",
    can_execute=None,
    priority_ready=None,
)
```

Use this to create a custom action. Long actions should be completed inside `execute`.

## FieldClaim

`FieldClaim` expresses "I should be switched in"; it is not an action. It only raises the target character's ordinary entry score. After the character enters, the planner still chooses an action from `actions`, a strict route/request, or `entry`.

```python
def combat_plan(self, context):
    claims = []
    if self.has_burst_window():
        claims.append(
            FieldClaim.high(
                reason="burst window active",
                expected_entry=ExpectedEntry(slot=ActionSlot.ULTIMATE),
            )
        )
    return self.plan(self.click_ultimate_action(), claims=claims)
```

Usage guidance:

- If only Q/E is available, no `FieldClaim` is needed; the action itself participates in scoring.
- Use `FieldClaim` when the character needs to "take the field back" later.
- Add `expected_entry` when a specific action should be prioritized after taking the field back.
- Multiple `FieldClaim` objects can express independent mechanic entry points; the planner does not add claim scores and selects the highest matching level.

## `combat_policies`

`combat_policies(context)` defines policies that remain active across the team lifecycle. The planner calls it when resetting the current team. It is suitable for permanent reservations, not a temporary window that appears only after this Q/E succeeds.

```python
def combat_policies(self, context: CombatContext):
    context.reserve_actions(
        [ActionReservation.for_action(zero, ActionSlot.SKILL)],
        reason="reserve Zero skill",
        until=Planner.NEVER_EXPIRES,
    )
```

## Coordination Requests

Publish coordination requests after an action executes successfully, or from `combat_policies()` for long-lived policies.

```python
def combat_plan(self, context):
    setup = self.click_skill_action()

    def entry():
        setup_result = yield setup
        if setup_result:
            context.request_route(
                [FollowupStep.for_action(zero, ActionSlot.SKILL, reason="Zero E")],
                reason="setup route",
            )

    return self.plan(setup, entry=entry)
```

Common APIs:

- `context.request_route(...)`: A fixed-order coordination route.

`FollowupStep.for_switch(target, wait_for_turn=True)` switches in and waits for the target's normal turn by default:

```python
context.request_route([
    FollowupStep.for_switch(a),
    FollowupStep.for_action(b, ActionSlot.ULTIMATE),
])
```

A finishes its normal entry flow before the route advances to B's ultimate.
If A is already on field, it still runs its turn. No first action or entry reaction is
required, and action permissions and reservations still apply. A turn uses the normal
flow termination conditions and action limit, including field-time fallback when no
action succeeds. Completion does not require a particular skill to succeed. Exceptions
do not complete the step; an expired or replaced route is not advanced afterward.

For arrival-only behavior, use `FollowupStep.for_switch(a, wait_for_turn=False)`.
The step completes on successful arrival or when the target is already on field.
A single-step route then releases normal flow; a multi-step route advances immediately,
**without waiting for A's turn or guaranteeing that A performs any action**.

Both modes inherit strict-route priority and lifetime. A new route replaces the existing
route, so this is not merely a higher-priority `request_switch()`. Use the latter for
ordinary independent switch requests.

- `context.request_switch(...)`: Request that the next ordinary dispatch switches to a character.
- `context.request_role(...)`: Request that the next ordinary dispatch switches to a character with a team role. When several characters match, ordinary switch scoring chooses one. It does not specify an action or interrupt the current entry flow.
- `context.reserve_actions(...)`: Reserve teammate actions.
- `context.request_tags(...)`: Request a number of actions with particular tags.

`request_role(Planner.Role.SUPPORT)` asks for any support-role character; `request_tags({Planner.ActionTag.SUPPORT})` asks for any support-type action. The former suits "bring in any support character", while the latter suits "let any teammate perform one healing/buff action".

```python
context.request_role(Planner.Role.SUPPORT, reason="need a support role")
```

## Behavior Summary

- Switch scoring and ordinary entry execution are separate.
- Scoring uses the highest-scoring ready action in `actions`, then adds `FieldClaim`, request, and field-preference scores.
- The current character's ordinary entry execution is controlled by `entry`; without an entry, actions run in declaration order.
- `priority_ready=False` only reduces switch attractiveness; it does not hard-block execution.
- `can_execute=False` is a hard block; a blocked entry action receives a failure result and is not actually executed.
- Strict routes, expected entries, and active requests take priority over ordinary entry flow.
- `ActionResult.tags` does not control entry flow.
