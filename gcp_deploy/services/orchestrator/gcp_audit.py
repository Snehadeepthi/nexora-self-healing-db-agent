"""
gcp_audit.py
Production Learn/Compliance backing store (Step 7/9 of the implementation
guide): implements the exact same method surface as the reference
audit.AuditLog -- log(), events(), mttd_seconds(), mttr_seconds(),
compliance_summary() -- backed by the real BigQuery db_ops.audit_log table
instead of an in-memory list, so act.py, predict.py, learn.py and anything
else that only ever calls audit_log.<method>(...) work unmodified.

mttd_seconds()/mttr_seconds() read from a short-lived per-instance cache
(populated by this instance's own log() calls) rather than re-querying
BigQuery, so they stay fast inside a single request. compliance_summary()
queries BigQuery directly, since it needs to reflect everything ever
recorded, not just this warm instance's slice of it -- that's the one method
here that's genuinely durable across restarts.
"""
import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timezone

from google.cloud import bigquery

logger = logging.getLogger(__name__)


class BigQueryAuditLog:
    def __init__(self, project_id=None, dataset="db_ops", table="audit_log", telemetry_table="telemetry"):
        self.project_id = project_id or os.environ["GCP_PROJECT"]
        self.dataset = dataset
        self.table = table
        self.client = bigquery.Client(project=self.project_id)
        self._table_ref = f"{self.project_id}.{self.dataset}.{self.table}"
        self._telemetry_ref = f"{self.project_id}.{self.dataset}.{telemetry_table}"
        self._cache = []  # this instance's own events, for fast mttd/mttr lookups
        # Reliability/latency safeguard: log() used to call
        # bigquery_client.insert_rows_json() synchronously inline, so every
        # audit event added a real network round-trip directly onto the
        # calling engine's own detection-to-remediation critical path.
        # Each engine now holds its own pipeline_lock (see pipeline.py), so
        # this stopped being a cross-engine blocking risk -- but it was
        # still needlessly serializing this engine's own onset-to-kill
        # latency behind BigQuery's response time. A single dedicated
        # background thread now drains a queue and performs the actual
        # insert_rows_json calls one at a time, in the same order they
        # were logged -- log() itself just enqueues and returns. events()/
        # mttd_seconds()/mttr_seconds() are unaffected: they only ever read
        # _cache, which log() still updates synchronously and immediately,
        # before the row is even enqueued.
        # Trade-off, stated honestly: a handful of rows queued but not yet
        # flushed could be lost if the instance is killed at that exact
        # moment (scale-down, deploy, crash). BigQuery durability was never
        # guaranteed to the caller synchronously either -- log() previously
        # raised only on a REJECTED insert, nothing protected against the
        # process dying between insert_rows_json returning and the
        # caller's next step -- so this narrows an existing window rather
        # than opening a new one. A failed background write is logged
        # loudly via Python logging (visible in Cloud Logging) instead of
        # raised, matching log_telemetry()'s existing best-effort philosophy.
        self._write_queue = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

    def _writer_loop(self):
        """Runs on a single dedicated daemon thread for the lifetime of
        this instance, draining self._write_queue one row at a time so
        BigQuery writes never block whichever engine's thread called
        log(). Errors are logged loudly (Cloud Logging picks these up)
        rather than raised -- there is no caller left to catch them by
        the time a queued write actually runs."""
        while True:
            row = self._write_queue.get()
            try:
                errors = self.client.insert_rows_json(self._table_ref, [row])
                if errors:
                    logger.error("BigQuery audit_log insert failed (background writer): %s | row=%s", errors, row)
            except Exception as e:
                logger.error("BigQuery audit_log insert raised (background writer): %s | row=%s", e, row)
            finally:
                self._write_queue.task_done()

    def log_telemetry(self, reading: dict):
        """Every Sense-stage reading, win or lose, lands here -- not just
        confirmed anomalies. This is what a future BigQuery ML ARIMA_PLUS
        model (the guide's Step 4) would train against once enough history
        accumulates; predict.py's threshold logic doesn't depend on this
        table existing, so a failure here never blocks the pipeline."""
        row = {
            "ts": reading.get("ts"),
            "active_blocked_sessions": reading.get("active_blocked_sessions"),
            "cpu_utilization_pct": reading.get("cpu_utilization_pct"),
            "in_maintenance_window": reading.get("in_maintenance_window", False),
        }
        try:
            self.client.insert_rows_json(self._telemetry_ref, [row])
        except Exception:
            pass  # best-effort; never let telemetry logging break the pipeline

    def log(self, event_type: str, **detail):
        ts = datetime.now(timezone.utc)
        incident_id = detail.get("incident_id")
        row = {
            "ts": ts.isoformat(),
            "event_type": event_type,
            "incident_id": incident_id,
            "detail": json.dumps(detail, default=str),
        }
        cached = {"ts": ts, "event_type": event_type, "incident_id": incident_id, "detail": detail}
        self._cache.append(cached)
        # Enqueue rather than write inline -- see the background-writer
        # note in __init__. Returns immediately; the actual BigQuery
        # write happens on the dedicated writer thread, in the same
        # order events were logged.
        self._write_queue.put(row)
        return cached

    def events(self):
        return list(self._cache)

    def mttd_seconds(self, incident_id: str):
        onset = self._find(incident_id, "anomaly_onset")
        detected = self._find(incident_id, "anomaly_detected")
        if onset and detected:
            return (detected["ts"] - onset["ts"]).total_seconds()
        return None

    def mttr_seconds(self, incident_id: str):
        detected = self._find(incident_id, "anomaly_detected")
        resolved = self._find(incident_id, "action_executed")
        if detected and resolved:
            return (resolved["ts"] - detected["ts"]).total_seconds()
        return None

    def _find(self, incident_id, event_type):
        for e in self._cache:
            if e["event_type"] == event_type and e["incident_id"] == incident_id:
                return e
        return None

    def compliance_summary(self):
        query = f"""
            SELECT event_type, COUNT(*) AS n
            FROM `{self._table_ref}`
            WHERE event_type IN (
              'action_executed', 'action_pending_approval',
              'action_blocked_unsigned', 'action_rejected_hallucination',
              'breaker_tripped'
            )
            GROUP BY event_type
        """
        counts = {row["event_type"]: row["n"] for row in self.client.query(query).result()}
        return {
            "total_actions_executed": counts.get("action_executed", 0),
            "tier3_actions_pending_or_approved": counts.get("action_pending_approval", 0),
            "unsigned_actions_blocked": counts.get("action_blocked_unsigned", 0),
            "hallucinated_actions_rejected": counts.get("action_rejected_hallucination", 0),
            "breaker_trips": counts.get("breaker_tripped", 0),
        }

    _ENGINE_PREFIX_RE = re.compile(r"^(oracle|alloydb|mysql)-(incident-\d+)$")
    _BARE_INCIDENT_RE = re.compile(r"^(incident-\d+)$")

    def incident_history(self, engine=None, limit=20):
        """Reconstructs each incident's full lifecycle (onset -> detected ->
        executed/failed/pending_approval -> report -> learning) for the
        dashboard's incident-history view -- /approvals/pending and the
        dashboard's client-side renderIncidents() only ever show OPEN
        incidents, so a fast Tier 1/2 auto-remediation is invisible the
        moment it resolves. This reconstructs the full timeline from the
        durable BigQuery record instead.

        incident_id is two-phase, the same way for all three engines:
        predict.py logs anomaly_onset/anomaly_detected under the bare
        "incident-N" tick id, then pipeline.py's run_cycle() renames it to
        "{engine.id}-incident-N" before anything from action_executed
        onward is logged. So grouping keys off the trailing "incident-N",
        and engine is read from whichever row in the group carries the
        prefix.
        """
        query = f"""
            SELECT ts, event_type, incident_id, detail
            FROM `{self._table_ref}`
            WHERE event_type IN (
                'anomaly_onset', 'anomaly_detected', 'action_executed',
                'action_failed', 'action_pending_approval',
                'action_rejected_hallucination', 'status_report_sent',
                'learning_event'
            )
            ORDER BY ts DESC
            LIMIT 2000
        """
        rows = list(self.client.query(query).result())

        def _event(row):
            ts = row["ts"]
            return {
                "ts": ts.isoformat() if ts else None,
                "event_type": row["event_type"],
                "detail": json.loads(row["detail"]) if row["detail"] else {},
            }

        # Pass 1: rows whose incident_id carries an engine prefix
        # unambiguously belong to that engine -- group these FIRST, keyed
        # by (engine, base_id). Two different engines' incidents can
        # legitimately share the same trailing tick number (both are just
        # Unix timestamps from independent Cloud Scheduler ticks), so
        # base_id alone is NOT a safe grouping key on its own -- verified
        # against real data where an Oracle kill_blocking_session action
        # showed up merged into an AlloyDB incident's timeline purely
        # because both happened to use the same tick number.
        groups = {}  # (engine_or_None, base_id) -> {"events": [...]}
        base_id_engines = {}  # base_id -> set of engines confirmed for it
        prefixed, bare = [], []
        for row in rows:
            iid = row["incident_id"] or ""
            m = self._ENGINE_PREFIX_RE.match(iid)
            if m:
                prefixed.append((row, m.group(1), m.group(2)))
            elif self._BARE_INCIDENT_RE.match(iid):
                bare.append((row, iid))
            # else: unrecognized incident_id shape -- skip rather than guess

        for row, row_engine, base_id in prefixed:
            key = (row_engine, base_id)
            groups.setdefault(key, {"events": []})["events"].append(_event(row))
            base_id_engines.setdefault(base_id, set()).add(row_engine)

        # Pass 2: bare onset/detected rows (predict.py logs these before
        # pipeline.py's run_cycle() namespaces incident_id per engine)
        # attach to the confirmed engine group for that base_id -- but
        # ONLY when exactly one engine claimed it. Zero claimants (never
        # got past onset/detected) or more than one (a genuine tick
        # collision between two engines) both get their own engine-less
        # group instead of being guessed into the wrong timeline.
        for row, base_id in bare:
            claimants = base_id_engines.get(base_id, set())
            key = (next(iter(claimants)), base_id) if len(claimants) == 1 else (None, base_id)
            groups.setdefault(key, {"events": []})["events"].append(_event(row))

        incidents = []
        for (g_engine, base_id), g in groups.items():
            if engine and g_engine and g_engine != engine:
                continue
            events = sorted(g["events"], key=lambda e: e["ts"] or "")
            event_types = {e["event_type"] for e in events}
            if "action_executed" in event_types:
                status = "resolved"
            elif "action_pending_approval" in event_types:
                status = "pending_approval"
            elif "action_failed" in event_types or "action_rejected_hallucination" in event_types:
                status = "failed"
            elif "learning_event" in event_types or "status_report_sent" in event_types:
                status = "no_action"  # detected and reasoned about, but no remediation was taken
            else:
                status = "open"
            incidents.append({
                "incident_id": base_id,
                "engine": g_engine,
                "status": status,
                "events": events,
            })

        incidents.sort(key=lambda i: i["events"][-1]["ts"] or "", reverse=True)
        return incidents[:limit]
