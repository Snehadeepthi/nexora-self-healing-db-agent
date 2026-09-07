"""
orchestrator.py
Wires Sense -> Predict -> Reason -> Act -> Learn into a single closed loop,
mirroring Step 8 of the implementation guide. In production, Cloud Scheduler
triggers Sense every 30s and each subsequent stage is invoked by a Pub/Sub
push; here it's a single `run_cycle()` you call in a loop (see demo.py).
"""

from act import GuardedExecutionEngine
from audit import AuditLog
from circuit_breaker import CircuitBreaker
from cost_guard import CostGuard
from notifications import EmailNotifier
from predict import AnomalyDetector
from reason import ReasonStage
from runbooks import RunbookStore
import learn
import sense


class Orchestrator:
    def __init__(self, oracle_client, db_executor):
        self.oracle_client = oracle_client
        self.audit_log = AuditLog()
        self.runbook_store = RunbookStore()
        self.notifier = EmailNotifier()
        self.detector = AnomalyDetector(self.audit_log)
        self.reasoner = ReasonStage(
            self.runbook_store, self.audit_log,
            breaker=CircuitBreaker(name="vertex_ai"),
            cost_guard=CostGuard(),
        )
        self.executor = GuardedExecutionEngine(
            db_executor, self.audit_log,
            breaker=CircuitBreaker(name="oracle_db"),
            notifier=self.notifier,
        )

    def run_cycle(self):
        """One Sense -> Predict -> Reason -> Act -> Learn tick. Returns a
        small trace dict for demo/logging purposes. Any Oracle outage during
        Sense is caught here (Risk 5) rather than crashing the loop."""
        try:
            reading = sense.poll(self.oracle_client)
        except Exception as e:
            self.audit_log.log("breaker_tripped", dependency="oracle_db_sense", detail=str(e))
            return {"stage": "sense", "outcome": "oracle_unreachable", "detail": str(e)}

        event = self.detector.score(reading)
        if event is None:
            return {"stage": "predict", "outcome": "no_confirmed_anomaly", "reading": reading}

        action_payload = self.reasoner.handle_anomaly_event(event)
        if action_payload is None:
            return {"stage": "reason", "outcome": "no_action_proposed", "event": event}

        result = self.executor.execute(action_payload)

        if result.status == "EXECUTED":
            healthy = True  # reference-implementation assumption; production
                              # re-polls telemetry to confirm before calling learn
            learn.learn_from_incident(
                event, action_payload, healthy, self.runbook_store, self.audit_log,
                notifier=self.notifier,
            )

        return {
            "stage": "act",
            "outcome": result.status,
            "detail": result.detail,
            "event": event,
            "action_payload": action_payload,
        }

    def approve_and_resolve(self, incident_id: str):
        """Call this once a human approves a Tier 3 action -- e.g. by
        following the link in the approval email notifications.py sent.
        Runs the same post-execution health-check + learn + status-report
        flow that run_cycle() uses for Tier 1/2 actions.

        action_payload already carries incident_id and query_text (set by
        llm_client.generate_action / reason.py), so it doubles as the
        `event` argument learn_from_incident() expects."""
        pending = self.executor.pending_approvals.get(incident_id)
        action_payload = pending[1] if pending else None

        result = self.executor.approve(incident_id)

        if result.status == "EXECUTED" and action_payload is not None:
            healthy = True  # production re-polls telemetry before this point
            learn.learn_from_incident(
                action_payload, action_payload, healthy, self.runbook_store,
                self.audit_log, notifier=self.notifier,
            )
        return result
