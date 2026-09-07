"""
guardrail_callbacks.py
Every risk mitigation that used to live inside act.py's GuardedExecutionEngine
and reason.py's ReasonStage, restated as ADK LlmAgent callbacks. config.py,
allowlist_governor.py, circuit_breaker.py, cost_guard.py are copied into this
package UNCHANGED -- this file is the only new code; it just moves the
choke-points from "a method act.py calls" to "a callback ADK calls" without
changing what any of them decide.

Callback -> risk mapping (see config.py's own docstring for the full risk
register):
  before_model_callback  -> Risk 6 (cost overrun) + Risk 5 (Vertex AI
                             coupling): gates the Reason stage's Gemini call
                             itself, exactly where reason.py's
                             cost_guard.check_and_record() / breaker.call()
                             sat before generate_action().
  before_tool_callback    -> Risk 7 (allowlist creep/signoff), Risk 1
                             (hallucination firewall + pre-state snapshot),
                             Tier 3 human-approval gate, Risk 5 (oracle_db
                             breaker open-check). Exactly
                             GuardedExecutionEngine.execute()'s body.
  after_tool_callback     -> Risk 3 (audit trail / MTTD-MTTR), Risk 5
                             (breaker success reset). Exactly
                             GuardedExecutionEngine._run()'s success path.
  on_tool_error_callback  -> Risk 5 (breaker failure recording), Risk 3
                             (action_failed audit entry). Exactly
                             GuardedExecutionEngine._run()'s except path.

A short note on the circuit breaker split: circuit_breaker.CircuitBreaker's
only public entry point is `.call(fn)`, which executes fn synchronously and
records success/failure around it -- a shape built for a single call site
(act.py used to call self.db.execute_statement directly). ADK splits "may
this call happen" (before_tool_callback) from "did it succeed"
(after_tool_callback / on_tool_error_callback) across a framework-managed
boundary, since the actual MCP tool execution happens *after*
before_tool_callback returns, not inside it. The open-state check below
calls `breaker._maybe_half_open()` (the same lazy OPEN->HALF_OPEN transition
`.call()` itself runs first) to peek at state without mutating failure
counts; success/failure recording after the fact both go through the real
public `.call()` method (wrapping a trivial success/re-raise thunk) so
circuit_breaker.py's own state-machine logic is exactly what decides trips
and resets -- nothing here reimplements its thresholds.
"""

import time

import config
from circuit_breaker import BreakerState, CircuitBreaker
from cost_guard import CostGuard

import db_registry
import db_tools

from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _thunk_raise(exc: Exception):
    def _inner():
        raise exc
    return _inner


class Guardrails:
    """One instance wired into agent.py's LlmAgent as its callback set, and
    shared with pipeline.py so the Tier 3 approve() path can see the same
    pending_approvals dict this class populates. Mirrors
    GuardedExecutionEngine + ReasonStage's constructor shape."""

    def __init__(self, audit_log, notifier=None, db_breaker=None,
                 alloydb_breaker=None, mysql_breaker=None, model_breaker=None, cost_guard=None, db_client=None):
        self.audit_log = audit_log
        if notifier is None:
            # Lazy, same reasoning as pipeline.py's audit_log/runbook_store/
            # notifier defaults: this module gets copied byte-identical into
            # gcp_deploy/services/orchestrator, which has no local
            # notifications.py (it always passes a real SlackNotifier
            # explicitly) -- importing it unconditionally at module load
            # would break that copy for a default branch it never takes,
            # the exact bug the original act.py hit once already.
            from notifications import EmailNotifier
            notifier = EmailNotifier()
        self.notifier = notifier
        self.db_breaker = db_breaker or CircuitBreaker(name="oracle_db")
        self.alloydb_breaker = alloydb_breaker or CircuitBreaker(name="alloydb")
        self.mysql_breaker = mysql_breaker or CircuitBreaker(name="mysql")
        self.model_breaker = model_breaker or CircuitBreaker(name="vertex_ai")
        self.cost_guard = cost_guard or CostGuard()
        # Lazy on purpose: ToolboxSyncClient's constructor blocks on a live
        # MCP handshake against TOOLBOX_URL, so a Guardrails instance can be
        # built (e.g. in unit tests) without a running Toolbox server --
        # the connection only has to exist once a snapshot is actually needed.
        self._db_client = db_client
        self.pending_approvals = {}   # incident_id -> {action_key, params, snapshot}
        self._in_flight_snapshots = {}  # incident_id -> snapshot, bridges before_tool -> after_tool
        # (engine_id, action_key) -> {since, since_incident_id, repeat_count, last_incident_id}
        # Set by expire_stale_approvals() the moment a Tier 3 proposal times out;
        # cleared only by clear_suppression() (an operator action, see main.py's
        # POST /suppressions/clear/<action_key>) -- see before_tool()'s Tier 3
        # branch and expire_stale_approvals()'s docstring for the full mechanism.
        self.suppressed = {}
        # engine_id -> consecutive confirmed-healthy tick count, used only by
        # record_tick_health()'s auto-clear below -- separate from suppressed
        # itself so a streak can keep building even on an engine with nothing
        # currently suppressed (harmless: the auto-clear loop just finds
        # nothing to clear that tick).
        self._consecutive_healthy_ticks = {}

    # ---------------------------------------------------------------
    # before_model_callback: gates the Reason stage's Gemini call itself.
    # Signature: (callback_context, llm_request) -> Optional[LlmResponse]
    # ---------------------------------------------------------------
    def before_model(self, callback_context, llm_request):
        incident_id = callback_context.state.get("incident_id", "unknown")

        if not self.cost_guard.check_and_record():
            reason = self.cost_guard.alerts[-1]
            self.audit_log.log("cost_capped", incident_id=incident_id, reason=reason)
            return _refusal_response(reason)

        self.model_breaker._maybe_half_open(time.time())
        if self.model_breaker.state == BreakerState.OPEN:
            detail = f"[{self.model_breaker.name}] circuit is OPEN -- refusing call"
            self.audit_log.log("breaker_tripped", incident_id=incident_id,
                                dependency="vertex_ai", detail=detail)
            return _refusal_response(detail)

        return None  # allow the real Gemini call through

    def after_model(self, callback_context, llm_response):
        # Record success/failure into the vertex_ai breaker's own state
        # machine via its public .call() -- see module docstring.
        if getattr(llm_response, "error_code", None):
            try:
                self.model_breaker.call(_thunk_raise(
                    RuntimeError(llm_response.error_message or llm_response.error_code)
                ))
            except Exception:
                pass
        else:
            self.model_breaker.call(lambda: True)
        return None

    # ---------------------------------------------------------------
    # before_tool_callback: Risk 7 signoff gate, Risk 1 hallucination
    # firewall + snapshot, Tier 3 approval gate, Risk 5 oracle_db breaker.
    # Signature: (tool, args, tool_context) -> Optional[dict]
    # A non-None dict short-circuits: the real tool never runs, and the
    # dict becomes the "tool result" the model sees.
    # ---------------------------------------------------------------
    def before_tool(self, tool, args, tool_context):
        if tool.name not in config.ALLOWLIST:
            return None  # not an allowlisted DB action (e.g. restart_listener) -- nothing to gate here

        incident_id = tool_context.state.get("incident_id", "unknown")

        try:
            action = config.get_action(tool.name)  # Risk 7: raises if unknown or unsigned
        except (KeyError, PermissionError) as e:
            self.audit_log.log("action_blocked_unsigned", incident_id=incident_id, detail=str(e))
            return {"status": "REJECTED", "detail": str(e)}

        # Cross-engine guard: the Reason stage's LLM agent is shared across all
        # three engines (see pipeline.py's build_orchestrators()) and every
        # engine's remediation tools are loaded into its ONE toolset (see
        # db_tools.load_remediation_toolset()'s REMEDIATION_TOOL_NAMES) -- so
        # nothing before this point stops the model from calling a tool that
        # belongs to a DIFFERENT engine than the incident it's actually
        # reasoning about. Confirmed live on 2026-08-24: an AlloyDB incident's
        # session called Oracle's kill_blocking_session, and the resulting
        # ALTER SYSTEM KILL SESSION statement was logged under an
        # alloydb-*-prefixed incident_id. incident_engine comes from the same
        # tool_context.state dict incident_id/query_text already use (stamped
        # in pipeline.py's _reason_and_act() from the orchestrator's OWN
        # self.engine.id, which is per-instance and never shared) -- so this
        # only ever fires for the real Sense->Predict->Reason->Act path; the
        # guardrail-gate demo shortcuts in main.py call before_tool() directly
        # with a hand-built context that has no "engine" key, so they're
        # intentionally unaffected.
        incident_engine = tool_context.state.get("engine")
        if incident_engine and action.engine != incident_engine:
            detail = (
                f"Tool '{tool.name}' belongs to engine '{action.engine}', but this "
                f"incident belongs to '{incident_engine}' -- refusing cross-engine call."
            )
            self.audit_log.log("action_rejected_wrong_engine", incident_id=incident_id,
                                action_key=tool.name, detail=detail)
            return {"status": "REJECTED", "detail": detail}

        firewall_error = self._validate_params(tool.name, action, args)
        if firewall_error:
            self.audit_log.log("action_rejected_hallucination", incident_id=incident_id, detail=firewall_error)
            return {"status": "REJECTED", "detail": firewall_error}

        if action.tier == config.Tier.TIER_3:
            suppression_key = (action.engine, tool.name)
            suppressed = self.suppressed.get(suppression_key)
            if suppressed is not None:
                # Flooding guard: a prior proposal of this EXACT action on
                # this engine already timed out unapproved (see
                # expire_stale_approvals() below) and set this flag.
                # Skipping the DB snapshot read here too, not just the
                # Slack ping -- the underlying anomaly is very often DB
                # load itself, so a repeat proposal every ~2 minutes
                # shouldn't also cost a fresh Toolbox round trip on top of
                # the audit entry.
                suppressed["repeat_count"] = suppressed.get("repeat_count", 0) + 1
                suppressed["last_incident_id"] = incident_id
                detail = (
                    f"'{tool.name}' on '{action.engine}' is suppressed after a prior Tier 3 "
                    f"approval_timeout (since incident {suppressed['since_incident_id']}) -- "
                    f"{suppressed['repeat_count']} repeat proposal(s) dropped without a fresh "
                    f"Slack ping. POST /suppressions/clear/{tool.name}?db={action.engine} (or the "
                    f"dashboard's reset control) once the underlying condition is confirmed handled."
                )
                self.audit_log.log("action_suppressed_after_timeout", incident_id=incident_id,
                                    action_key=tool.name, detail=detail)
                return {"status": "SUPPRESSED", "detail": detail}

            snapshot = self._snapshot(action, args)
            self.pending_approvals[incident_id] = {
                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,
                "query_text": tool_context.state.get("query_text", ""),
                "created_at": time.time(),
            }
            self.audit_log.log("action_pending_approval", incident_id=incident_id,
                                action_key=tool.name, tier=int(action.tier))
            email = self.notifier.send_approval_request(
                {"action_key": tool.name, "params": args, "incident_id": incident_id}, action
            )
            self.audit_log.log("approval_notification_sent", incident_id=incident_id, to=email.to)
            return {
                "status": "PENDING_APPROVAL",
                "detail": f"'{tool.name}' requires Tier 3 approval",
                "incident_id": incident_id,
            }

        snapshot = self._snapshot(action, args)
        breaker = self._breaker_for(action.engine)
        breaker._maybe_half_open(time.time())
        if breaker.state == BreakerState.OPEN:
            detail = f"[{breaker.name}] circuit is OPEN -- refusing call"
            self.audit_log.log("breaker_tripped", incident_id=incident_id,
                                dependency=breaker.name, detail=detail)
            return {"status": "BREAKER_OPEN", "detail": detail}

        self._in_flight_snapshots[incident_id] = snapshot
        return None  # allow the real MCP tool call through

    def after_tool(self, tool, args, tool_context, tool_response):
        if tool.name not in config.ALLOWLIST:
            return None

        incident_id = tool_context.state.get("incident_id", "unknown")

        # ADK calls after_tool_callback even when before_tool_callback itself
        # short-circuited the call (Tier 3 PENDING_APPROVAL, a rejected
        # action, a breaker-open block) -- treating that short-circuit dict
        # as if it were the tool's own response. Only a call before_tool()
        # actually let through populates _in_flight_snapshots (see
        # before_tool()'s final "allow" branch) -- its absence here means
        # the real tool never ran, so logging "action_executed" would be a
        # false audit entry with a fabricated statement. Confirmed live on
        # 2026-08-26: a Tier 3 alloydb_reset_all_connections request logged
        # BOTH a legitimate action_pending_approval AND a spurious
        # action_executed (empty snapshot, since none was ever stored) in
        # the same tick, 22 seconds before the real, human-approved
        # execution -- and the same empty-snapshot pattern was found for 22
        # more historical Tier 3 requests going back to 2026-08-24.
        if incident_id not in self._in_flight_snapshots:
            return None

        action = config.ALLOWLIST[tool.name]
        self._breaker_for(action.engine).call(lambda: True)  # success -- resets/closes the breaker

        snapshot = self._in_flight_snapshots.pop(incident_id)
        statement = action.statement_template.format(**args)
        self.audit_log.log("action_executed", incident_id=incident_id,
                            action_key=tool.name, statement=statement, snapshot=snapshot)
        return None

    def on_tool_error(self, tool, args, tool_context, error):
        if tool.name not in config.ALLOWLIST:
            return {"status": "REJECTED", "detail": f"execution failed: {error}"}

        incident_id = tool_context.state.get("incident_id", "unknown")

        # Same guard as after_tool(): only a call before_tool() actually let
        # through populates _in_flight_snapshots, so this only records a
        # genuine failure (and only trips the real breaker) for a call that
        # actually executed -- not a short-circuited one ADK still routes
        # through this callback.
        if incident_id not in self._in_flight_snapshots:
            return {"status": "REJECTED", "detail": f"execution failed: {error}"}

        try:
            self._breaker_for(config.ALLOWLIST[tool.name].engine).call(_thunk_raise(error))
        except Exception:
            pass

        snapshot = self._in_flight_snapshots.pop(incident_id)
        self.audit_log.log("action_failed", incident_id=incident_id, detail=str(error), snapshot=snapshot)
        return {"status": "REJECTED", "detail": f"execution failed: {error}"}

    # ---------------------------------------------------------------
    # helpers
    # ---------------------------------------------------------------
    def _validate_params(self, action_key: str, action, args: dict):
        """Field-by-field param_schema validation -- identical in spirit to
        llm_client.generate_action()'s hallucination firewall. ADK's own
        function-calling schema already constrains what Gemini can send;
        this is defense in depth against schema drift or an unexpected
        extra/missing field."""
        for name, expected_type in action.param_schema.items():
            if name not in args:
                return f"Missing required param '{name}' for action '{action_key}'."
            value = args[name]
            if expected_type is int and isinstance(value, float) and value.is_integer():
                value = int(value)
                args[name] = value
            if not isinstance(value, expected_type):
                return (f"Param '{name}' for action '{action_key}' failed schema "
                        f"validation (expected {expected_type.__name__}, got {type(value).__name__}).")
        extra = set(args) - set(action.param_schema)
        if extra:
            return f"Action '{action_key}' called with un-declared params {sorted(extra)}."
        return None

    def _get_db_client(self, engine=None):
        """engine=None keeps the original shared-client behavior (used
        by any caller that predates the Toolbox split); a real engine
        routes to db_tools.get_sync_client_for(engine), which is Oracle-
        transparent (same client as before) and AlloyDB/MySQL-split (the
        new Cloud Run Toolbox instance) -- see that function's docstring."""
        if engine is not None:
            return db_tools.get_sync_client_for(engine)
        if self._db_client is None:
            self._db_client = db_tools.get_sync_client()
        return self._db_client

    def _breaker_for(self, engine_id: str):
        """Route to the per-engine circuit breaker. Oracle actions (the
        default engine) keep using self.db_breaker unchanged -- same
        object, same name, same behavior the existing tests rely on -- so
        an outage in any one engine never trips or blocks the others
        (Risk 5, scoped per dependency instead of one breaker coupling
        all engines together)."""
        if engine_id == "alloydb":
            return self.alloydb_breaker
        if engine_id == "mysql":
            return self.mysql_breaker
        return self.db_breaker

    def expire_stale_approvals(self, engine_id: str) -> list:
        """Tier 3 TTL safeguard: a pending_approvals entry with no human
        response within config.TIER3_APPROVAL_TTL_SECONDS is auto-expired
        rather than left to accumulate indefinitely -- during something
        like a lock cascade, an unresolved Tier 3 action sitting forever in
        PENDING_APPROVAL means nothing ever acts while the underlying
        problem can keep getting worse. Called once per engine at the start
        of that engine's run_cycle() tick (pipeline.py), scoped to THIS
        engine's own incident_ids (namespaced "{engine_id}-...", see
        pipeline.py's run_cycle()) so one engine's tick never touches
        another engine's still-live pending approval. Does not attempt to
        auto-escalate to a lower-tier fallback action -- it aborts safely
        and notifies loudly instead, so a human still finds out even though
        nothing executed.
        Flooding guard (external review, closed 2026-09-02): on its own,
        clearing the stale entry above wasn't enough -- if the underlying
        anomaly is still active, Predict proposes a FRESH Tier 3 action
        with a new incident_id on a later tick, roughly every ~2 minutes
        for as long as the anomaly persists, and before_tool() used to
        page Slack again every single time. Every timeout below now also
        arms self.suppressed[(engine_id, action_key)]; before_tool()'s
        Tier 3 branch checks that flag BEFORE creating a new
        pending_approvals entry or sending another approval request, so a
        prolonged outage produces exactly one PENDING_APPROVAL ping and
        one approval_timeout ping, then silence (still audit-logged) until
        an operator calls clear_suppression() -- see main.py's
        POST /suppressions/clear/<action_key>.
        Residual, disclosed limitation: suppression is keyed on the exact
        (engine_id, action_key) pair, so a different allowlisted action
        proposed for the same underlying condition still notifies
        separately; and nothing auto-clears the flag when the condition
        actually resolves -- an operator has to confirm that and reset it,
        by design, so a fix that silently stopped applying itself can't go
        unnoticed.
        """
        now = time.time()
        expired_ids = [
            incident_id for incident_id, pending in self.pending_approvals.items()
            if incident_id.startswith(f"{engine_id}-")
            and now - pending.get("created_at", now) > config.TIER3_APPROVAL_TTL_SECONDS
        ]
        for incident_id in expired_ids:
            pending = self.pending_approvals.pop(incident_id)
            detail = (
                f"Tier 3 action '{pending['action_key']}' was never approved within "
                f"{config.TIER3_APPROVAL_TTL_SECONDS}s -- auto-expired without executing."
            )
            self.audit_log.log("approval_timeout", incident_id=incident_id,
                                action_key=pending["action_key"], detail=detail)
            self.notifier.send_approval_timeout(
                {"action_key": pending["action_key"], "params": pending["params"],
                 "incident_id": incident_id}, detail,
            )
            suppression_key = (engine_id, pending["action_key"])
            self.suppressed[suppression_key] = {
                "since": now, "since_incident_id": incident_id,
                "repeat_count": 0, "last_incident_id": incident_id,
            }
        return expired_ids

    def clear_suppression(self, engine_id: str, action_key: str = None) -> list:
        """Manual reset for the Tier 3 flooding guard above -- an operator
        calls this (main.py's POST /suppressions/clear/<action_key>) once
        they've confirmed the underlying condition is actually handled, or
        was a false alarm. Nothing in this process clears a suppression on
        its own -- see expire_stale_approvals()'s docstring for why that's
        deliberate. Clears one (engine_id, action_key) pair, or every
        suppression on engine_id if action_key is omitted. Returns the
        (engine_id, action_key) pairs actually cleared."""
        if action_key is not None:
            keys = [(engine_id, action_key)] if (engine_id, action_key) in self.suppressed else []
        else:
            keys = [k for k in self.suppressed if k[0] == engine_id]
        for key in keys:
            cleared = self.suppressed.pop(key)
            self.audit_log.log(
                "suppression_cleared", incident_id=cleared.get("last_incident_id", "unknown"),
                action_key=key[1],
                detail=f"Manually reset by operator after {cleared.get('repeat_count', 0)} suppressed repeat(s).",
            )
        return keys

    def list_suppressed(self, engine_id: str) -> list:
        """Backs main.py's GET /suppressions -- same read-only-mirror
        pattern as pending_approvals/GET /approvals/pending."""
        return [
            {
                "action_key": action_key, "since": v["since"],
                "since_incident_id": v["since_incident_id"],
                "repeat_count": v.get("repeat_count", 0),
                "last_incident_id": v.get("last_incident_id"),
            }
            for (eng, action_key), v in self.suppressed.items() if eng == engine_id
        ]

    def record_tick_health(self, engine_id: str, anomaly_detected: bool) -> list:
        """Tier 3 suppression auto-recovery. Called once per engine per tick
        from pipeline.py's run_cycle(), right after Predict resolves whether
        this reading is a confirmed anomaly -- NOT called at all on a tick
        that never reaches Predict (breaker open, Sense poll failure), so an
        outage can't quietly un-suppress itself just by going quiet; only a
        genuinely completed, clean Predict read counts.

        A single anomaly_detected=True resets the streak to zero. Once
        config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS consecutive clean reads
        land, every suppression still armed for this engine is cleared and
        logged as action_suppression_auto_cleared -- distinct from the
        operator-driven suppression_cleared event (see clear_suppression),
        so the audit trail always shows which path did the clearing. Returns
        the (engine_id, action_key) pairs actually cleared, same shape
        clear_suppression() returns, or [] on a tick that didn't trigger a
        clear."""
        if anomaly_detected:
            self._consecutive_healthy_ticks[engine_id] = 0
            return []

        streak = self._consecutive_healthy_ticks.get(engine_id, 0) + 1
        self._consecutive_healthy_ticks[engine_id] = streak
        if streak < config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS:
            return []

        self._consecutive_healthy_ticks[engine_id] = 0
        keys = [k for k in self.suppressed if k[0] == engine_id]
        cleared = []
        for key in keys:
            entry = self.suppressed.pop(key)
            self.audit_log.log(
                "action_suppression_auto_cleared",
                incident_id=entry.get("last_incident_id", "unknown"),
                action_key=key[1],
                detail=(
                    f"Auto-cleared after {config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS} "
                    f"consecutive healthy ticks on '{engine_id}' with no confirmed "
                    f"anomaly; had been suppressed since incident "
                    f"{entry.get('since_incident_id', 'unknown')} after "
                    f"{entry.get('repeat_count', 0)} repeat proposal(s)."
                ),
            )
            cleared.append((engine_id, key[1]))
        return cleared

    def _snapshot(self, action, args: dict) -> dict:
        """Risk 1: pre-state snapshot before ANYTHING executes (Tier 1/2/3
        alike), same as GuardedExecutionEngine._snapshot(). Degrades to an
        honest 'unavailable' marker rather than blocking the guardrail gate
        on a Toolbox connection issue -- same pattern oracle_client.py uses
        for its own best-effort v$sysmetric read.

        Takes the resolved AllowlistedAction (not just its key) so the
        snapshot is read from the SAME engine the action is about to run
        against -- this used to call Oracle's describe_state
        unconditionally for every action regardless of engine, which would
        have produced a mislabeled Oracle snapshot on every AlloyDB
        action's audit trail instead of an honest 'unavailable'."""
        engine = db_registry.get_engine(action.engine)
        try:
            pre_state = db_tools.describe_state(self._get_db_client(engine), engine)
        except Exception as e:
            pre_state = {"active_blocked_sessions_snapshot": "unavailable", "error": str(e)}
        return {"pre_state": pre_state, "action": {"action_key": action.action_key, "params": dict(args)}}


def _refusal_response(reason: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=(
            f"[guardrail] Falling back to alert-only mode: {reason}"
        ))]),
        turn_complete=True,
    )
