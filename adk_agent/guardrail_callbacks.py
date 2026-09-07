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
                 model_breaker=None, cost_guard=None, db_client=None):
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
        self.model_breaker = model_breaker or CircuitBreaker(name="vertex_ai")
        self.cost_guard = cost_guard or CostGuard()
        # Lazy on purpose: ToolboxSyncClient's constructor blocks on a live
        # MCP handshake against TOOLBOX_URL, so a Guardrails instance can be
        # built (e.g. in unit tests) without a running Toolbox server --
        # the connection only has to exist once a snapshot is actually needed.
        self._db_client = db_client
        self.pending_approvals = {}   # incident_id -> {action_key, params, snapshot}
        self._in_flight_snapshots = {}  # incident_id -> snapshot, bridges before_tool -> after_tool

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

        firewall_error = self._validate_params(tool.name, action, args)
        if firewall_error:
            self.audit_log.log("action_rejected_hallucination", incident_id=incident_id, detail=firewall_error)
            return {"status": "REJECTED", "detail": firewall_error}

        snapshot = self._snapshot(tool.name, args)

        if action.tier == config.Tier.TIER_3:
            self.pending_approvals[incident_id] = {
                "action_key": tool.name, "params": dict(args), "snapshot": snapshot,
                "query_text": tool_context.state.get("query_text", ""),
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

        self.db_breaker._maybe_half_open(time.time())
        if self.db_breaker.state == BreakerState.OPEN:
            detail = f"[{self.db_breaker.name}] circuit is OPEN -- refusing call"
            self.audit_log.log("breaker_tripped", incident_id=incident_id,
                                dependency="oracle_db", detail=detail)
            return {"status": "BREAKER_OPEN", "detail": detail}

        self._in_flight_snapshots[incident_id] = snapshot
        return None  # allow the real MCP tool call through

    def after_tool(self, tool, args, tool_context, tool_response):
        if tool.name not in config.ALLOWLIST:
            return None

        incident_id = tool_context.state.get("incident_id", "unknown")
        self.db_breaker.call(lambda: True)  # success -- resets/closes the breaker

        snapshot = self._in_flight_snapshots.pop(incident_id, {})
        statement = config.ALLOWLIST[tool.name].statement_template.format(**args)
        self.audit_log.log("action_executed", incident_id=incident_id,
                            action_key=tool.name, statement=statement, snapshot=snapshot)
        return None

    def on_tool_error(self, tool, args, tool_context, error):
        if tool.name not in config.ALLOWLIST:
            return {"status": "REJECTED", "detail": f"execution failed: {error}"}

        incident_id = tool_context.state.get("incident_id", "unknown")
        try:
            self.db_breaker.call(_thunk_raise(error))
        except Exception:
            pass

        snapshot = self._in_flight_snapshots.pop(incident_id, {})
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

    def _get_db_client(self):
        if self._db_client is None:
            self._db_client = db_tools.get_sync_client()
        return self._db_client

    def _snapshot(self, action_key: str, args: dict) -> dict:
        """Risk 1: pre-state snapshot before ANYTHING executes (Tier 1/2/3
        alike), same as GuardedExecutionEngine._snapshot(). Degrades to an
        honest 'unavailable' marker rather than blocking the guardrail gate
        on a Toolbox connection issue -- same pattern oracle_client.py uses
        for its own best-effort v$sysmetric read."""
        try:
            pre_state = db_tools.describe_state(self._get_db_client())
        except Exception as e:
            pre_state = {"active_blocked_sessions_snapshot": "unavailable", "error": str(e)}
        return {"pre_state": pre_state, "action": {"action_key": action_key, "params": dict(args)}}


def _refusal_response(reason: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=(
            f"[guardrail] Falling back to alert-only mode: {reason}"
        ))]),
        turn_complete=True,
    )
