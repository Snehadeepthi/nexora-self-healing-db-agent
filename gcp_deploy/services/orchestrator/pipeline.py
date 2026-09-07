"""
pipeline.py
Wires Sense -> Predict -> Reason/Act -> Learn into a single closed loop --
the ADK-native equivalent of orchestrator.py. Sense and Predict stay exactly
as deterministic as they were (predict.py is copied unchanged; sense.py is
copied unchanged and driven by a tiny shim so it never has to know its
'oracle_client' is now backed by MCP Toolbox instead of oracledb/a
simulator). Reason+Act is now one ADK agent turn instead of two separate
manual calls -- see agent.py's docstring for why that's a genuine upgrade,
not just a relabeling. Learn stays exactly as deterministic as before too.

audit_log/runbook_store/notifier are constructor-injected (defaulting to
this package's local in-memory stand-ins) rather than hardcoded, the same
dependency-injection shape act.py/reason.py always used -- this is what
lets this exact file be copied byte-identical into
gcp_deploy/services/orchestrator, where main.py instead injects
gcp_audit.BigQueryAuditLog / gcp_runbooks.BigQueryRunbookStore /
gcp_notifications.SlackNotifier. Nothing in this file needs to know or care
which one it's holding, same as every other guardrail file in this project.
"""

import asyncio
import threading
import time

import config
import db_registry
import db_tools
import learn
import sense
from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import build_agent
from circuit_breaker import BreakerState
from guardrail_callbacks import Guardrails
from predict import AnomalyDetector

APP_NAME = "self-healing-db-agent"
USER_ID = "orchestrator"


class _ReadingSource:
    """Satisfies sense.poll()'s `oracle_client.poll()` interface -- the same
    shape OracleSimulator and oracle_client.OracleClient satisfy -- while
    actually reading through MCP Toolbox. sense.py never needs to know or
    care which backend is behind it."""

    def __init__(self, db_client, engine=None):
        self._db_client = db_client
        self.engine = engine

    def poll(self):
        return db_tools.poll_telemetry(self._db_client, engine=self.engine)


_TELEMETRY_HISTORY_MAXLEN = 50


class AdkOrchestrator:
    def __init__(self, audit_log=None, runbook_store=None, notifier=None,
                 engine=None, guardrails=None, agent=None, runner=None,
                 session_service=None, pipeline_lock=None):
        if audit_log is None:
            from audit import AuditLog
            audit_log = AuditLog()
        if runbook_store is None:
            from runbooks import RunbookStore
            runbook_store = RunbookStore()
        if notifier is None:
            from notifications import EmailNotifier
            notifier = EmailNotifier()

        self.audit_log = audit_log
        self.runbook_store = runbook_store
        self.notifier = notifier
        self.engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
        self.detector = AnomalyDetector(self.audit_log)
        self.guardrails = guardrails or Guardrails(self.audit_log, notifier=self.notifier)
        self.agent = agent or build_agent(self.guardrails)
        self.session_service = session_service or InMemorySessionService()
        self.runner = runner or Runner(
            agent=self.agent, app_name=APP_NAME, session_service=self.session_service
        )
        self._db_client = db_tools.get_sync_client_for(self.engine)
        self._reading_source = _ReadingSource(self._db_client, engine=self.engine)
        self.telemetry_history = []  # dashboard-only read path, see get_telemetry_history()
        # Reliability safeguard: Cloud Run's max_instance_request_concurrency
        # was raised from 1 (fully serialized) to let the dashboard's several
        # simultaneous read-only GETs stop starving each other. That's safe
        # for reads, but run_cycle() and approve_and_resolve() both mutate
        # shared in-process state (the anomaly counter, breaker state,
        # guardrails.pending_approvals) with no other synchronization -- this
        # lock is what keeps two overlapping /tick calls (e.g. a Cloud
        # Scheduler tick landing mid-"Scan Now" click) from racing on that
        # state, now that more than one request can be in flight at once.
        self._pipeline_lock = pipeline_lock or threading.Lock()

    # -----------------------------------------------------------------
    # One Sense -> Predict -> Reason/Act -> Learn tick. Returns the same
    # small trace-dict shape orchestrator.py's run_cycle() does, for
    # demo.py/main.py parity.
    # -----------------------------------------------------------------
    def run_cycle(self) -> dict:
        # Reliability safeguard: the Sense-stage poll now shares
        # guardrails.db_breaker with the Act-stage remediation calls
        # (before_tool/after_tool/on_tool_error in guardrail_callbacks.py)
        # instead of being a raw, unguarded try/except. A sustained Oracle
        # outage used to mean every single Cloud-Scheduler tick (every 60s)
        # kept hammering a dependency that was already known to be down;
        # now CONSECUTIVE failures here count toward the SAME breaker
        # remediation actions use, and once it trips OPEN, Sense
        # short-circuits immediately (no connection attempt at all) until
        # config.CIRCUIT_BREAKER_RESET_SECONDS elapses. This also fixes the
        # "Breaker trips" dashboard tile (gcp_audit.py's compliance query,
        # counting event_type='breaker_tripped'): previously every raw Sense
        # failure -- even the very first one, nowhere near tripping anything
        # -- was mislabeled 'breaker_tripped' in the audit trail, inflating
        # that counter with noise. Only a genuine breaker-OPEN short-circuit
        # is logged as 'breaker_tripped' now; an ordinary (not-yet-tripped)
        # poll failure is logged as 'sense_poll_failed' instead.
        # Reliability safeguard #2: the whole cycle now runs under
        # self._pipeline_lock (see __init__) so two overlapping /tick calls
        # -- possible now that Cloud Run concurrency is > 1 -- can't race on
        # the anomaly counter or breaker state. A tick queued behind another
        # simply waits; it never sees half-updated state.
        with self._pipeline_lock:
            self.guardrails.expire_stale_approvals(self.engine.id)
            db_breaker = self.guardrails._breaker_for(self.engine.id)
            db_breaker._maybe_half_open(time.time())
            if db_breaker.state == BreakerState.OPEN:
                detail = f"[{db_breaker.name}] circuit is OPEN -- refusing Sense poll until reset"
                self.audit_log.log("breaker_tripped", dependency=f"{self.engine.id}_db_sense", detail=detail)
                return {"stage": "sense", "outcome": "breaker_open", "detail": detail}

            try:
                reading = db_breaker.call(lambda: sense.poll(self._reading_source))
            except Exception as e:
                self.audit_log.log("sense_poll_failed", dependency=f"{self.engine.id}_db_sense", detail=str(e))
                return {"stage": "sense", "outcome": f"{self.engine.id}_unreachable", "detail": str(e)}

            self.telemetry_history.append(reading)
            if len(self.telemetry_history) > _TELEMETRY_HISTORY_MAXLEN:
                self.telemetry_history.pop(0)

            event = self.detector.score(reading)
            self.guardrails.record_tick_health(self.engine.id, anomaly_detected=event is not None)
            if event is None:
                return {"stage": "predict", "outcome": "no_confirmed_anomaly", "reading": reading}
            event = dict(event)  # don't mutate predict.py's own returned dict -- also lets us tag it
            event["incident_id"] = f"{self.engine.id}-{event['incident_id']}"
            event["engine"] = self.engine.id

            if event.get("metric") == "active_blocked_sessions":
                event["blocking_session"] = self._lookup_blocking_session()
            elif event.get("metric") == "max_query_duration_seconds":
                event["runaway_query"] = self._lookup_runaway_query()

            # active_blocked_sessions is intentionally engine-agnostic in
            # predict.py (same detection logic for both engines), so its
            # query_text alone can't distinguish Oracle from AlloyDB --
            # without this tag, an AlloyDB blocking-session incident would
            # retrieve Oracle's ALTER SYSTEM-based runbook via an exact
            # cosine match (distance 0) instead of the correct
            # pg_terminate_backend-based one. Every other new AlloyDB
            # signal already has a distinct query_text, so no tagging is
            # needed there.
            lookup_query_text = event["query_text"]
            metric = event.get("metric")
            if self.engine.id == "alloydb":
                if metric == "active_blocked_sessions":
                    lookup_query_text = "AlloyDB " + lookup_query_text + " pg_stat_activity pg_locks postgres backend"
                elif metric == "idle_in_transaction_count":
                    lookup_query_text = "AlloyDB " + lookup_query_text + " pg_stat_activity postgres backend idle in transaction"
                elif metric == "connection_pct":
                    lookup_query_text = "AlloyDB " + lookup_query_text + " pg_stat_activity postgres connection storm max_connections"
            elif self.engine.id == "mysql":
                if metric == "active_blocked_sessions":
                    lookup_query_text = "MySQL " + lookup_query_text + " performance_schema data_lock_waits innodb processlist"
                elif metric == "idle_in_transaction_count":
                    lookup_query_text = "MySQL " + lookup_query_text + " innodb processlist idle in transaction connections"
                elif metric == "connection_pct":
                    lookup_query_text = "MySQL " + lookup_query_text + " innodb processlist connection storm max_connections"

            runbook, distance = self.runbook_store.nearest(lookup_query_text)
            outcome = asyncio.run(self._reason_and_act(event, runbook))

            if outcome["status"] is None:
                return {"stage": "reason", "outcome": "no_action_proposed", "event": event}

            if outcome["status"] == "EXECUTED":
                # Reference-implementation assumption carried over verbatim from
                # orchestrator.py: a real deployment re-polls telemetry before
                # calling learn() to confirm the fix actually worked.
                learn.learn_from_incident(
                    event, outcome["action_payload"], True,
                    self.runbook_store, self.audit_log, notifier=self.notifier,
                )
                # Deterministic follow-up (never LLM-selected): right after a
                # real alloydb_kill_runaway_query execution, tighten
                # statement_timeout to prevent the same runaway pattern from
                # recurring immediately. Genuinely reversible -- see
                # _apply_statement_timeout_followup and main.py's /rollback
                # endpoint.
                if (self.engine.id == "alloydb"
                        and outcome["action_payload"]
                        and outcome["action_payload"].get("action_key") == "alloydb_kill_runaway_query"):
                    self._apply_statement_timeout_followup(outcome["action_payload"]["incident_id"])

            return {
                "stage": "act", "outcome": outcome["status"], "detail": outcome.get("detail"),
                "event": event, "action_payload": outcome.get("action_payload"),
            }

    def _apply_statement_timeout_followup(self, incident_id: str) -> None:
        """Deterministic follow-up (never LLM-selected) after a real
        alloydb_kill_runaway_query execution: tighten statement_timeout to
        prevent the same runaway pattern from recurring immediately.
        Captures the exact prior value first (db_tools.get_statement_timeout)
        so it can be replayed verbatim on rollback -- see main.py's
        /rollback endpoint. Best-effort: a failure here doesn't undo or fail
        the kill that already succeeded, it just means the follow-up
        tightening didn't happen this time -- logged honestly either way."""
        action_key = "alloydb_set_statement_timeout"
        new_value = "5000ms"
        try:
            prior_value = db_tools.get_statement_timeout(self._db_client, self.engine)
            params = {"statement_timeout_value": new_value}
            db_tools.run_remediation_directly(self._db_client, action_key, params)
            self.audit_log.log(
                "action_executed", incident_id=incident_id, action_key=action_key,
                statement=config.ALLOWLIST[action_key].statement_template.format(**params),
                snapshot={
                    "pre_state": {"statement_timeout": prior_value},
                    "action": {"action_key": action_key, "params": params},
                },
            )
        except Exception as e:
            self.audit_log.log(
                "action_failed", incident_id=incident_id, action_key=action_key,
                detail=f"statement_timeout follow-up failed: {e}",
            )

    async def _reason_and_act(self, event: dict, runbook) -> dict:
        incident_id = event["incident_id"]
        session = await self.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=incident_id,
            state={
                "incident_id": incident_id,
                "query_text": event.get("query_text", ""),
                "engine": self.engine.id,
            },
        )
        message = types.Content(role="user", parts=[types.Part(text=_incident_prompt(event, runbook))])

        events = []
        async for e in self.runner.run_async(
            user_id=USER_ID, session_id=session.id, new_message=message
        ):
            events.append(e)
        return _extract_outcome(events, incident_id)

    def _lookup_blocking_session(self) -> dict:
        """Risk-informed grounding for kill_blocking_session's required
        sid/serial# params -- see db_tools.find_blocking_session's docstring
        for why the LLM can't be left to infer these. Degrades to an honest
        {"error": ...} marker rather than blocking the whole incident on a
        Toolbox hiccup, same pattern guardrail_callbacks.py's own
        best-effort _snapshot() uses."""
        try:
            return db_tools.find_blocking_session(self._db_client, engine=self.engine)
        except Exception as e:
            return {"error": str(e)}

    def _lookup_runaway_query(self) -> dict:
        """Same anti-hallucination grounding as _lookup_blocking_session,
        for alloydb_kill_runaway_query's required pid param. AlloyDB-only
        today -- Oracle has no runaway-query action requiring a grounded
        pid this way."""
        try:
            return db_tools.find_runaway_query(self._db_client, engine=self.engine)
        except Exception as e:
            return {"error": str(e)}

    def get_telemetry_history(self) -> list:
        """Dashboard-only read path (main.py's /telemetry/history) -- an
        in-memory ring buffer of the last _TELEMETRY_HISTORY_MAXLEN
        successful Sense-stage readings, across BOTH manually-triggered and
        Cloud-Scheduler-triggered ticks, so the dashboard's chart reflects
        real automatic polling too, not just clicks from the dashboard
        itself. Same in-memory-only caveat as everything else this class
        holds (see main.py's module docstring): resets on cold start."""
        return list(self.telemetry_history)

    # -----------------------------------------------------------------
    # Tier 3 human approval -- mirrors act.py's GuardedExecutionEngine.
    # approve() calling self._run() directly, never re-asking the LLM.
    # -----------------------------------------------------------------
    def approve_and_resolve(self, incident_id: str) -> dict:
        # Same self._pipeline_lock as run_cycle() -- an operator clicking
        # Approve at the exact moment a Scheduler tick is mid-flight must not
        # interleave with it; both mutate guardrails/audit state.
        with self._pipeline_lock:
            pending = self.guardrails.pending_approvals.pop(incident_id, None)
            if pending is None:
                return {"status": "REJECTED", "detail": "no pending approval for this incident"}

            action_key, params, snapshot = pending["action_key"], pending["params"], pending["snapshot"]
            try:
                if action_key == "restart_listener":
                    db_tools.restart_listener(**params)  # always raises -- see its docstring
                else:
                    db_tools.run_remediation_directly(self._db_client, action_key, params)
            except Exception as e:
                self.audit_log.log("action_failed", incident_id=incident_id, detail=str(e), snapshot=snapshot)
                return {"status": "REJECTED", "detail": f"execution failed: {e}"}

            statement = config.ALLOWLIST[action_key].statement_template.format(**params)
            self.audit_log.log("action_executed", incident_id=incident_id, action_key=action_key,
                                statement=statement, snapshot=snapshot)

            action_payload = {
                "action_key": action_key, "params": params, "incident_id": incident_id,
                "query_text": pending.get("query_text", ""),
            }
            learn.learn_from_incident(
                action_payload, action_payload, True,
                self.runbook_store, self.audit_log, notifier=self.notifier,
            )
            return {"status": "EXECUTED", "detail": statement}


def build_orchestrators(audit_log=None, runbook_store=None, notifier=None) -> dict:
    """Factory for the multi-database pipeline: builds ONE shared Guardrails
    instance (so its pending_approvals dict, breakers, and cost_guard state
    are the same object no matter which engine's incident is being approved
    -- see guardrail_callbacks.py's per-engine _breaker_for() routing, added
    specifically so this sharing is safe), ONE shared ADK agent/runner/
    session_service pair (the LLM reasoning stage doesn't need per-engine
    duplication, just per-engine-tagged incident_ids -- see run_cycle()'s
    namespacing of event["incident_id"]), and ONE AdkOrchestrator per
    registered engine (detector, reading_source, and telemetry_history all
    stay genuinely per-engine, so Oracle and AlloyDB anomaly counters/
    history never mix). Returns {engine_id: AdkOrchestrator}."""
    if audit_log is None:
        from audit import AuditLog
        audit_log = AuditLog()
    if runbook_store is None:
        from runbooks import RunbookStore
        runbook_store = RunbookStore()
    if notifier is None:
        from notifications import EmailNotifier
        notifier = EmailNotifier()
    shared_guardrails = Guardrails(audit_log, notifier=notifier)
    shared_agent = build_agent(shared_guardrails)
    shared_session_service = InMemorySessionService()
    shared_runner = Runner(
        agent=shared_agent, app_name=APP_NAME, session_service=shared_session_service
    )
    orchestrators = {}
    for engine_id, engine in db_registry.DB_REGISTRY.items():
        orchestrators[engine_id] = AdkOrchestrator(
            audit_log=audit_log, runbook_store=runbook_store, notifier=notifier,
            engine=engine, guardrails=shared_guardrails, agent=shared_agent,
            runner=shared_runner, session_service=shared_session_service,
            pipeline_lock=threading.Lock(),
        )
    return orchestrators


def _incident_prompt(event: dict, runbook) -> str:
    lines = [
        "Incident details:",
        f"  engine: {event.get('engine')} -- ONLY call a tool tagged [{event.get('engine')}] "
        f"in the allowlist below for this incident. Calling a different engine's tool will "
        f"be rejected.",
        f"  metric: {event.get('metric')}",
        f"  status: {event.get('status')}",
        f"  value: {event.get('value')}",
        f"  query_text: {event.get('query_text')}",
    ]
    blocker = event.get("blocking_session")
    if blocker and "error" not in blocker:
        if "pid" in blocker:
            lines.append(
                f"  blocking_session: pid={blocker['pid']} "
                f"-- this is the real Postgres backend currently holding the lock, "
                f"looked up directly from pg_stat_activity. If you call "
                f"alloydb_kill_blocking_session, use exactly this pid -- do not "
                f"guess or invent one."
            )
        elif "processlist_id" in blocker:
            lines.append(
                f"  blocking_session: processlist_id={blocker['processlist_id']} "
                f"-- this is the real MySQL connection currently holding the lock, "
                f"looked up directly from performance_schema.data_lock_waits. If "
                f"you call mysql_kill_blocking_session, use exactly this "
                f"processlist_id -- do not guess or invent one."
            )
        else:
            lines.append(
                f"  blocking_session: sid={blocker['sid']} serial={blocker['serial']} "
                f"-- this is the real session currently holding the lock, looked up "
                f"directly from v$session. If you call kill_blocking_session, use "
                f"exactly these values -- do not guess or invent a sid/serial."
            )
    runaway = event.get("runaway_query")
    if runaway and "error" not in runaway:
        if "processlist_id" in runaway:
            lines.append(
                f"  runaway_query: processlist_id={runaway['processlist_id']} "
                f"-- this is the real MySQL connection running the longest-active "
                f"query right now, looked up directly from information_schema."
                f"processlist. If you call mysql_kill_runaway_query, use exactly "
                f"this processlist_id -- do not guess or invent one."
            )
        else:
            lines.append(
                f"  runaway_query: pid={runaway['pid']} "
                f"-- this is the real Postgres backend running the longest-active "
                f"query right now, looked up directly from pg_stat_activity. If you "
                f"call alloydb_kill_runaway_query, use exactly this pid -- do not "
                f"guess or invent one."
            )
    if event.get("metric") == "idle_in_transaction_count":
        if event.get("engine") == "mysql":
            lines.append(
                "  detection note: connections were counted as idle-in-transaction "
                "using a 300 second cutoff (matching mysql_poll_telemetry's query) "
                "-- pass idle_seconds=300 to mysql_terminate_idle_in_transaction "
                "unless you have a specific reason to choose otherwise."
            )
        else:
            lines.append(
                "  detection note: backends were counted as idle-in-transaction using "
                "a 300 second cutoff (matching alloydb_poll_telemetry's query) -- pass "
                "idle_seconds=300 to alloydb_terminate_idle_in_transaction unless you "
                "have a specific reason to choose otherwise."
            )
    lines.append(
        f"  nearest known runbook: {runbook.title if runbook else 'none'}"
        f" -- {runbook.body if runbook else ''}"
    )
    return "\n".join(lines) + "\n"


def _extract_outcome(events, incident_id: str) -> dict:
    """Walks the event stream for the tool call the model made (if any) and
    the function_response that came back. Short-circuited results
    (REJECTED/PENDING_APPROVAL/BREAKER_OPEN) come from guardrail_callbacks.py
    itself and always carry an explicit 'status' key; a genuinely-executed
    real MCP tool call's response is Toolbox's own raw result and never
    happens to have that key -- so its absence (with a tool call present)
    is what "EXECUTED" is inferred from."""
    action_key, args, status, detail = None, None, None, None
    for e in events:
        for fc in e.get_function_calls():
            action_key, args = fc.name, dict(fc.args or {})
        for fr in e.get_function_responses():
            resp = fr.response or {}
            status = resp.get("status", status)
            detail = resp.get("detail", detail)

    if action_key is None:
        return {"status": None}

    return {
        "status": status or "EXECUTED",
        "detail": detail,
        "action_payload": {"action_key": action_key, "params": args, "incident_id": incident_id},
    }