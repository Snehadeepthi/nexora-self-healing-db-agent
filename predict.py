"""
predict.py
Local/dev version of the Predict stage (Step 4). In production this is the
BigQuery ML ARIMA_PLUS model + ML.DETECT_ANOMALIES streaming query; here it's
a lightweight threshold model so the reference pipeline runs without a live
BigQuery project.

Risk 2 mitigation:
  - a reading taken during a maintenance window is never counted as an
    anomaly candidate, and resets any in-progress streak
  - a SINGLE anomalous reading never triggers a detection -- only
    config.CONSECUTIVE_ANOMALY_THRESHOLD consecutive anomalous readings do
Risk 3 mitigation:
  - the first reading of a confirmed run is logged as "anomaly_onset" and the
    confirming tick as "anomaly_detected", so audit.py can compute true MTTD
"""

import config


class AnomalyDetector:
    def __init__(self, audit_log, blocked_sessions_threshold=8):
        self.audit_log = audit_log
        self.blocked_sessions_threshold = blocked_sessions_threshold
        self._consecutive = 0
        self._incident_id = None

    def score(self, reading: dict):
        """Returns a confirmed anomaly event dict, or None if not (yet)
        confirmed / suppressed."""
        is_candidate = (
            not reading["in_maintenance_window"]
            and reading["active_blocked_sessions"] >= self.blocked_sessions_threshold
        )

        if not is_candidate:
            self._consecutive = 0
            self._incident_id = None
            return None

        if self._consecutive == 0:
            self._incident_id = f"incident-{reading['tick']}"
            self.audit_log.log(
                "anomaly_onset", incident_id=self._incident_id, reading=reading
            )

        self._consecutive += 1

        if self._consecutive < config.CONSECUTIVE_ANOMALY_THRESHOLD:
            return None  # not confirmed yet -- Risk 2 guardrail

        event = {
            "incident_id": self._incident_id,
            "metric": "active_blocked_sessions",
            "status": "sustained_high",
            "root_cause": "unconfirmed",   # Reason stage fills this in via RAG + LLM
            "value": reading["active_blocked_sessions"],
            "query_text": "active_blocked_sessions sustained_high unconfirmed",
        }
        self.audit_log.log(
            "anomaly_detected", incident_id=self._incident_id, reading=reading
        )
        self._consecutive = 0  # reset so the next confirmed run gets a fresh id
        return event
