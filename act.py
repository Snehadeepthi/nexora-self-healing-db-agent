"""
act.py
Local/dev version of the Act stage (Step 6): the Guarded Execution Engine.
Mirrors services/executor/main.py in the production reference implementation.

Mitigations implemented here:
  Risk 1 (wrong-diagnosis blast radius) -- every action snapshots pre-state
     before executing, and Tier 3 actions never auto-execute.
  Risk 7 (allowlist creep)              -- config.get_action() itself refuses
     to return an action without a valid signoff; act.py never bypasses it.
  Risk 5 (multi-vendor coupling)        -- the actual DB call is wrapped in a
     circuit breaker.
  Risk 3 (latency visibility)           -- every outcome is timestamped into
     the audit log so MTTR is derivable.

Email notifications: the moment a Tier 3 action needs a human, an approval
email goes out via notifications.EmailNotifier so nobody has to be staring at
a dashboard to catch it (see notifications.py).
"""

from dataclasses import dataclass

import config
from circuit_breaker import CircuitBreaker, CircuitOpenError
from notifications import EmailNotifier


@dataclass
class ExecutionResult:
    status: str        # EXECUTED | PENDING_APPROVAL | REJECTED | BREAKER_OPEN
    detail: str = ""


class GuardedExecutionEngine:
    def __init__(self, db, audit_log, breaker=None, notifier=None):
        self.db = db                     # object with .execute_statement() / .describe_state()
        self.audit_log = audit_log
        self.breaker = breaker or CircuitBreaker(name="oracle_db")
        self.notifier = notifier or EmailNotifier()
        self.pending_approvals = {}      # incident_id -> (action, payload, snapshot), Tier 3 queue

    def execute(self, action_payload: dict) -> ExecutionResult:
        incident_id = action_payload.get("incident_id")

        try:
            action = config.get_action(action_payload["action_key"])
        except (KeyError, PermissionError) as e:
            self.audit_log.log(
                "action_blocked_unsigned", incident_id=incident_id, detail=str(e)
            )
            return ExecutionResult("REJECTED", str(e))

        # Risk 1: snapshot pre-state before anything Tier 1/2 executes, so the
        # action is reversible in principle even without a full Flashback op.
        snapshot = self._snapshot(action_payload)

        if action.tier == config.Tier.TIER_3:
            self.pending_approvals[incident_id] = (action, action_payload, snapshot)
            self.audit_log.log(
                "action_pending_approval", incident_id=incident_id,
                action_key=action.action_key, tier=int(action.tier),
            )
            email = self.notifier.send_approval_request(action_payload, action)
            self.audit_log.log(
                "approval_notification_sent", incident_id=incident_id, to=email.to
            )
            return ExecutionResult(
                "PENDING_APPROVAL", f"'{action.action_key}' requires Tier 3 approval"
            )

        return self._run(action, action_payload, snapshot)

    def approve(self, incident_id: str) -> ExecutionResult:
        """Simulates a human clicking the Slack 'Approve' button (Step 6)."""
        if incident_id not in self.pending_approvals:
            return ExecutionResult("REJECTED", "no pending approval for this incident")
        action, action_payload, snapshot = self.pending_approvals.pop(incident_id)
        return self._run(action, action_payload, snapshot)

    def _snapshot(self, action_payload: dict) -> dict:
        return {"pre_state": self.db.describe_state(), "action": dict(action_payload)}

    def _run(self, action, action_payload, snapshot) -> ExecutionResult:
        statement = action.statement_template.format(**action_payload["params"])
        incident_id = action_payload.get("incident_id")
        try:
            self.breaker.call(self.db.execute_statement, statement)
        except CircuitOpenError as e:
            self.audit_log.log(
                "breaker_tripped", incident_id=incident_id,
                dependency="oracle_db", detail=str(e),
            )
            return ExecutionResult("BREAKER_OPEN", str(e))
        except Exception as e:
            self.audit_log.log(
                "action_failed", incident_id=incident_id, detail=str(e), snapshot=snapshot
            )
            return ExecutionResult("REJECTED", f"execution failed: {e}")

        self.audit_log.log(
            "action_executed", incident_id=incident_id,
            action_key=action.action_key, statement=statement, snapshot=snapshot,
        )
        return ExecutionResult("EXECUTED", statement)
