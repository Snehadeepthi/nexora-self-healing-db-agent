"""
audit.py
Risk 3 mitigation (MTTD/MTTR visibility) and Risk 8 mitigation (compliance /
audit exposure). In-memory stand-in for the BigQuery `db_ops.audit_log` table
described in the implementation guide (Step 9) -- every anomaly, action,
approval, rejection, and learning event is appended here as the system of
record.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json


@dataclass
class AuditEvent:
    ts: str
    event_type: str   # anomaly_onset | anomaly_detected | action_executed |
                        # action_pending_approval | action_blocked_unsigned |
                        # action_rejected_hallucination | action_failed |
                        # breaker_tripped | cost_capped | learning_event |
                        # post_fix_unhealthy
    detail: dict = field(default_factory=dict)


class AuditLog:
    def __init__(self):
        self._events: list = []

    def log(self, event_type: str, **detail):
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            detail=detail,
        )
        self._events.append(event)
        return event

    def events(self):
        return list(self._events)

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(asdict(e)) for e in self._events)

    # ---- Risk 3: MTTD / MTTR derived straight from the audit trail ----
    def mttd_seconds(self, incident_id: str):
        onset = self._find(incident_id, "anomaly_onset")
        detected = self._find(incident_id, "anomaly_detected")
        if onset and detected:
            return (_parse(detected.ts) - _parse(onset.ts)).total_seconds()
        return None

    def mttr_seconds(self, incident_id: str):
        detected = self._find(incident_id, "anomaly_detected")
        resolved = self._find(incident_id, "action_executed")
        if detected and resolved:
            return (_parse(resolved.ts) - _parse(detected.ts)).total_seconds()
        return None

    def _find(self, incident_id, event_type):
        for e in self._events:
            if e.event_type == event_type and e.detail.get("incident_id") == incident_id:
                return e
        return None

    # ---- Risk 8: compliance summary, the artifact you'd hand to an auditor ----
    def compliance_summary(self):
        return {
            "total_actions_executed": len(
                [e for e in self._events if e.event_type == "action_executed"]
            ),
            "tier3_actions_pending_or_approved": len(
                [e for e in self._events if e.event_type == "action_pending_approval"]
            ),
            "unsigned_actions_blocked": len(
                [e for e in self._events if e.event_type == "action_blocked_unsigned"]
            ),
            "hallucinated_actions_rejected": len(
                [e for e in self._events if e.event_type == "action_rejected_hallucination"]
            ),
            "breaker_trips": len(
                [e for e in self._events if e.event_type == "breaker_tripped"]
            ),
        }


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)
