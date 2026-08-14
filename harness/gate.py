"""The interjection gate.

Deterministic and model-free: on every wake it evaluates the pack's
declarative rules against engine-computed facts and decides whether
the interviewer acts at all. The model is consulted only after a rule
has fired, and is told which hint level to phrase. Every evaluation —
including the silent ones — is written to the transcript with the
rule that fired (or none) and the facts it saw.

Default outcome: silence. A rule fires only if its wake kind matches,
its condition holds, it is off cooldown, it has fires left, and (when
it counts toward the interjection budget) budget remains. Exactly one
rule fires per wake: the highest priority, first-declared winner.

Hint levels never skip: whatever a rule asks for is clamped to at most
one step above the highest level already used on the current task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RuleLedger:
    fires: int = 0
    last_fire_t: Optional[float] = None


@dataclass
class Decision:
    rule: Optional[Any]  # the pack Rule that fired, or None
    action: str          # "speak" | "advance" | "silence"
    hint_level: int
    evaluations: List[Dict[str, Any]] = field(default_factory=list)


class Gate:
    def __init__(self, pack: Any) -> None:
        self.pack = pack
        self.ledger: Dict[str, RuleLedger] = {r.id: RuleLedger() for r in pack.rules}
        self._order = sorted(pack.rules, key=lambda r: (-r.priority, r.index))

    def evaluate(self, facts: Dict[str, Any], now_t: float) -> Decision:
        wake = facts["kind"]
        evaluations: List[Dict[str, Any]] = []
        chosen = None
        for rule in self._order:
            if "any" not in rule.on and wake not in rule.on:
                continue
            ledger = self.ledger[rule.id]
            reason = None
            if not rule.when.as_bool(facts):
                reason = "when_false"
            elif rule.max_fires > 0 and ledger.fires >= rule.max_fires:
                reason = "max_fires"
            elif (
                rule.cooldown_s > 0
                and ledger.last_fire_t is not None
                and now_t - ledger.last_fire_t < rule.cooldown_s
            ):
                reason = "cooldown"
            elif rule.counts_toward_budget and facts["budget_left"] <= 0:
                reason = "budget"
            elif chosen is not None:
                reason = "shadowed"
            evaluations.append(
                {"rule": rule.id, "fired": reason is None, "reason": reason or "fired"}
            )
            if reason is None:
                chosen = rule

        if chosen is None:
            return Decision(rule=None, action="silence", hint_level=0, evaluations=evaluations)

        hint_level = 0
        if chosen.action == "speak" and chosen.hint != "none":
            current = int(facts["hint_level"])
            ceiling = len(self.pack.ladder)
            if ceiling > 0:
                wanted = current + 1 if chosen.hint == "escalate" else int(chosen.hint)
                hint_level = max(1, min(wanted, current + 1, ceiling))
        return Decision(rule=chosen, action=chosen.action, hint_level=hint_level, evaluations=evaluations)

    def commit(self, decision: Decision, now_t: float) -> None:
        """Record a fired decision in the ledger. Call only when the
        decision was actually acted on."""
        if decision.rule is None:
            return
        ledger = self.ledger[decision.rule.id]
        ledger.fires += 1
        ledger.last_fire_t = now_t
