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

Multi-signal (added for AlloyDB coverage): originally this only ever
tracked one signal (active_blocked_sessions), which happened to work for
both engines since db_tools.poll_telemetry() normalizes both engines'
readings into the same key. It now tracks four independent signals, each
with its OWN consecutive-reading streak (so a blip in one signal can't
piggyback a different signal's streak toward confirmation, and vice
versa). A reading missing a given key simply never becomes a candidate for
that signal -- Oracle readings, which don't carry the three AlloyDB-only
keys, are completely unaffected by this change; existing behavior for
active_blocked_sessions is unchanged (same threshold, same debounce, same
maintenance-window suppression).
"""
import config


# (metric_key, status_label, threshold_config_attr, query_text) -- checked
# in this order each tick; only the FIRST confirmed signal is returned per
# tick (same "one event per tick" contract the original single-signal
# version had).
_SIGNALS = [
    ("active_blocked_sessions", "sustained_high", "blocked_sessions_threshold",
     "active_blocked_sessions sustained_high unconfirmed"),
    ("max_query_duration_seconds", "runaway_query", "runaway_query_seconds_threshold",
     "runaway_query long_running query_duration high cpu backend"),
    ("idle_in_transaction_count", "idle_in_transaction_buildup", "idle_in_transaction_count_threshold",
     "connections stuck idle-in-transaction"),
    ("connection_pct", "connection_storm", "connection_pct_threshold",
     "connection storm -- last-resort reset requested"),
]


class AnomalyDetector:
    def __init__(self, audit_log, blocked_sessions_threshold=8,
                 runaway_query_seconds_threshold=None,
                 idle_in_transaction_count_threshold=None,
                 connection_pct_threshold=None):
        self.audit_log = audit_log
        self.blocked_sessions_threshold = blocked_sessions_threshold
        self.runaway_query_seconds_threshold = (
            runaway_query_seconds_threshold
            if runaway_query_seconds_threshold is not None
            else config.RUNAWAY_QUERY_SECONDS_THRESHOLD
        )
        self.idle_in_transaction_count_threshold = (
            idle_in_transaction_count_threshold
            if idle_in_transaction_count_threshold is not None
            else config.IDLE_IN_TRANSACTION_COUNT_THRESHOLD
        )
        self.connection_pct_threshold = (
            connection_pct_threshold
            if connection_pct_threshold is not None
            else config.CONNECTION_PCT_THRESHOLD
        )
        # One independent consecutive-reading streak per signal, keyed by
        # metric name -- e.g. {"active_blocked_sessions": {"consecutive": 1,
        # "incident_id": "incident-172"}}. Kept separate so a streak in one
        # signal never counts toward confirming a different signal.
        self._streaks = {}

    def score(self, reading: dict):
        """Returns a confirmed anomaly event dict, or None if not (yet)
        confirmed / suppressed. Only ever returns ONE event per call, for
        the first signal (in _SIGNALS order) that reaches its own
        consecutive-threshold streak this tick."""
        if reading.get("in_maintenance_window"):
            self._streaks = {}
            return None

        candidates = {}
        for metric, _status, threshold_attr, _query_text in _SIGNALS:
            if metric not in reading:
                continue
            if reading[metric] >= getattr(self, threshold_attr):
                candidates[metric] = True

        # A signal must be over threshold on CONSECUTIVE ticks, not just at
        # some point in the past -- drop the streak the moment it drops
        # below threshold.
        for metric in list(self._streaks):
            if metric not in candidates:
                del self._streaks[metric]

        confirmed_event = None
        for metric, status, _threshold_attr, query_text in _SIGNALS:
            if metric not in candidates:
                continue
            streak = self._streaks.setdefault(metric, {"consecutive": 0, "incident_id": None})
            if streak["consecutive"] == 0:
                streak["incident_id"] = f"incident-{reading['tick']}"
                self.audit_log.log(
                    "anomaly_onset", incident_id=streak["incident_id"], reading=reading
                )
            streak["consecutive"] += 1
            if streak["consecutive"] < config.CONSECUTIVE_ANOMALY_THRESHOLD:
                continue  # not confirmed yet for this signal -- Risk 2 guardrail
            if confirmed_event is None:
                confirmed_event = {
                    "incident_id": streak["incident_id"],
                    "metric": metric,
                    "status": status,
                    "root_cause": "unconfirmed",  # Reason stage fills this in via RAG + LLM
                    "value": reading[metric],
                    "query_text": query_text,
                }
                self.audit_log.log(
                    "anomaly_detected", incident_id=streak["incident_id"], reading=reading
                )
            streak["consecutive"] = 0  # reset so the next confirmed run gets a fresh id

        return confirmed_event
