"""
reason.py
Local/dev version of the Reason stage (Step 5): RAG retrieval + constrained
LLM diagnosis, wrapped with the Risk 5 (circuit breaker) and Risk 6 (cost
guard) mitigations that sit in front of every Vertex AI call in production.
"""

from circuit_breaker import CircuitBreaker, CircuitOpenError
from cost_guard import CostGuard
import llm_client


class ReasonStage:
    def __init__(self, runbook_store, audit_log, breaker=None, cost_guard=None):
        self.runbook_store = runbook_store
        self.audit_log = audit_log
        self.breaker = breaker or CircuitBreaker(name="vertex_ai")
        self.cost_guard = cost_guard or CostGuard()

    def handle_anomaly_event(self, event: dict):
        if not self.cost_guard.check_and_record():
            self.audit_log.log(
                "cost_capped", incident_id=event["incident_id"],
                reason=self.cost_guard.alerts[-1],
            )
            return None  # Risk 6: fall back to alert-only, no LLM call

        runbook, distance = self.runbook_store.nearest(event["query_text"])

        try:
            action_payload = self.breaker.call(llm_client.generate_action, event, runbook)
        except CircuitOpenError as e:
            self.audit_log.log(
                "breaker_tripped", incident_id=event["incident_id"],
                dependency="vertex_ai", detail=str(e),
            )
            return None  # Risk 5: fall back to alert-only
        except llm_client.HallucinatedActionError as e:
            self.audit_log.log(
                "action_rejected_hallucination", incident_id=event["incident_id"],
                detail=str(e),
            )
            return None  # Risk 1: never let a non-allowlisted action through

        action_payload["incident_id"] = event["incident_id"]
        action_payload["nearest_runbook_distance"] = distance
        return action_payload
