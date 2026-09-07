"""
test_safety.py (adk_agent edition)
The CI safety gate for the ADK + MCP Toolbox rebuild -- same role
cloudbuild.yaml gives the original test_safety.py (a single failure here
blocks the deploy), same risk-register mapping, adapted to test
guardrail_callbacks.Guardrails directly instead of act.py's
GuardedExecutionEngine. Everything that didn't change (config.py,
allowlist_governor.py, circuit_breaker.py, cost_guard.py, predict.py,
runbooks.py, learn.py, notifications.py -- all copied byte-identical from
the original reference implementation) is tested exactly the same way the
original test_safety.py tested it; only the tests that exercised act.py's
execute()/approve() are rewritten against before_tool/after_tool/
on_tool_error, since that's where the equivalent logic now lives.

Nothing here needs a live Gemini or Toolbox connection -- same "runs
anywhere, no live GCP needed" property the original test_safety.py had.
Run: pytest test_safety.py -v
"""

import time

import pytest

import config
import db_tools
import learn
import sense
from allowlist_governor import SIGNOFF_LEDGER, audit_unsigned_actions
from audit import AuditLog
from circuit_breaker import CircuitBreaker, CircuitOpenError, BreakerState
from cost_guard import CostGuard
from guardrail_callbacks import Guardrails
from notifications import EmailNotifier
from predict import AnomalyDetector
from runbooks import RunbookStore


class _FakeTool:
    def __init__(self, name):
        self.name = name


class _FakeCtx:
    def __init__(self, incident_id, query_text=""):
        self.state = {"incident_id": incident_id, "query_text": query_text}


class _FailingReadingSource:
    def poll(self):
        raise RuntimeError("ORA-12541: TNS:no listener (simulated outage)")


# ---------------------------------------------------------------------------
# Risk 7: allowlist creep / IAM erosion -- unchanged from the original
# ---------------------------------------------------------------------------
def test_every_allowlist_entry_has_a_valid_signoff():
    unsigned = audit_unsigned_actions()
    assert unsigned == [], f"Unsigned allowlist entries block deploy: {unsigned}"


def test_action_without_signoff_is_rejected_by_before_tool():
    # Temporarily revoke a signoff to prove the callback actually enforces
    # it via config.get_action(), not just that the ledger happens to be
    # populated.
    saved = SIGNOFF_LEDGER.pop("flush_shared_pool")
    try:
        g = Guardrails(AuditLog())
        result = g.before_tool(_FakeTool("flush_shared_pool"), {}, _FakeCtx("t1"))
        assert result["status"] == "REJECTED"
    finally:
        SIGNOFF_LEDGER["flush_shared_pool"] = saved


# ---------------------------------------------------------------------------
# Cross-engine guard: the shared ADK agent's one toolset holds every engine's
# remediation tools (see db_tools.REMEDIATION_TOOL_NAMES) -- before_tool()
# must independently confirm a tool belongs to the SAME engine as the
# incident it's being called for. Regression coverage for the 2026-08-24
# incident where an AlloyDB incident's session called Oracle's
# kill_blocking_session and nothing blocked it.
# ---------------------------------------------------------------------------
def test_cross_engine_tool_call_is_rejected_by_before_tool():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("alloydb-incident-999")
    ctx.state["engine"] = "alloydb"
    result = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 1, "serial": 1}, ctx)
    assert result["status"] == "REJECTED"
    assert "engine" in result["detail"].lower()


def test_same_engine_tool_call_is_not_blocked_by_the_cross_engine_guard():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("alloydb-incident-1000")
    ctx.state["engine"] = "alloydb"
    result = g.before_tool(_FakeTool("alloydb_kill_blocking_session"), {"pid": 123}, ctx)
    assert result is None or result.get("status") != "REJECTED"


# ---------------------------------------------------------------------------
# Regression coverage for a second, distinct bug found in the same rehearsal:
# ADK invokes after_tool_callback even when before_tool_callback itself
# short-circuited the call (Tier 3 PENDING_APPROVAL, a rejected action, a
# breaker-open block) -- treating that short-circuit dict as if it were the
# tool's own response. Confirmed live on 2026-08-26 against 22 historical
# incidents plus a fresh rehearsal: after_tool() was logging a false
# "action_executed" entry (fabricated statement, empty snapshot) at the
# moment of the short-circuit, well before any real execution.
# ---------------------------------------------------------------------------
def test_short_circuited_before_tool_call_does_not_log_a_false_action_executed():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("t-shortcircuit")
    result = g.before_tool(_FakeTool("restart_listener"), {"listener_name": "L"}, ctx)
    assert result["status"] == "PENDING_APPROVAL"
    assert "t-shortcircuit" not in g._in_flight_snapshots

    g.after_tool(_FakeTool("restart_listener"), {"listener_name": "L"}, ctx, result)
    executed = [e for e in g.audit_log.events() if e.event_type == "action_executed"]
    assert executed == []
    assert "t-shortcircuit" in g.pending_approvals


def test_after_tool_logs_action_executed_for_a_genuinely_allowed_call():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("t-genuine")
    result = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 1, "serial": 1}, ctx)
    assert result is None
    assert "t-genuine" in g._in_flight_snapshots

    g.after_tool(_FakeTool("kill_blocking_session"), {"sid": 1, "serial": 1}, ctx, {"result": "ok"})
    executed = [e for e in g.audit_log.events() if e.event_type == "action_executed"]
    assert len(executed) == 1
    assert executed[0].detail["incident_id"] == "t-genuine"
    assert "t-genuine" not in g._in_flight_snapshots


# ---------------------------------------------------------------------------
# Regression coverage for a third bug found while preparing MySQL's rehearsal:
# idle_in_transaction_buildup and connection_storm are signals ANY engine can
# trigger (predict.py's detection logic is shared), but their query_text used
# to hardcode the literal word "AlloyDB" -- so a genuine MySQL incident's own
# prompt to the LLM would falsely say "AlloyDB connections stuck
# idle-in-transaction", very plausibly causing the model to call AlloyDB's
# action instead of MySQL's (which the cross-engine guard would then
# correctly, but unhelpfully, reject -- MySQL Tier 2/3 would never actually
# remediate). Also added: the incident prompt now states engine explicitly
# rather than relying on inference from query_text wording at all.
# ---------------------------------------------------------------------------
def test_shared_signal_query_text_is_engine_neutral():
    from predict import _SIGNALS
    for metric, status, _threshold_attr, query_text in _SIGNALS:
        if status in ("idle_in_transaction_buildup", "connection_storm"):
            assert "alloydb" not in query_text.lower()
            assert "mysql" not in query_text.lower()


def test_incident_prompt_states_the_engine_explicitly():
    from pipeline import _incident_prompt
    from runbooks import RunbookStore
    rb, _dist = RunbookStore().nearest("connection storm -- last-resort reset requested")
    event = {
        "metric": "connection_pct", "status": "connection_storm", "value": 0.15,
        "query_text": "connection storm -- last-resort reset requested", "engine": "mysql",
    }
    prompt = _incident_prompt(event, rb)
    assert "engine: mysql" in prompt
    assert "alloydb" not in prompt.lower()


# ---------------------------------------------------------------------------
# Risk 1: autonomous action on wrong diagnosis (hallucination firewall + tiering)
# ---------------------------------------------------------------------------
def test_agent_tool_surface_cannot_contain_an_unlisted_action():
    # Gemini's own function-calling can only ever propose a tool that's in
    # the agent's declared tools list -- a structurally stronger guarantee
    # than the original reference implementation's JSON-mode text parsing,
    # which needed llm_client.py's own runtime check to catch a fabricated
    # action_key. There is no equivalent "the model invented a tool name
    # that doesn't exist" path to test here; what remains testable (and
    # what the tests below cover) is the field-level firewall for a real
    # tool called with malformed arguments.
    assert set(db_tools.REMEDIATION_TOOL_NAMES) <= set(config.ALLOWLIST)


def test_missing_required_param_is_rejected_before_execution():
    g = Guardrails(AuditLog())
    result = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 101}, _FakeCtx("t2a"))
    assert result["status"] == "REJECTED"


def test_wrong_typed_param_is_rejected_before_execution():
    g = Guardrails(AuditLog())
    result = g.before_tool(
        _FakeTool("kill_blocking_session"), {"sid": "not-an-int", "serial": 202}, _FakeCtx("t2b")
    )
    assert result["status"] == "REJECTED"


def test_undeclared_extra_param_is_rejected_before_execution():
    g = Guardrails(AuditLog())
    result = g.before_tool(_FakeTool("flush_shared_pool"), {"unexpected": 1}, _FakeCtx("t2c"))
    assert result["status"] == "REJECTED"


def test_tier3_action_never_auto_executes():
    g = Guardrails(AuditLog())
    result = g.before_tool(
        _FakeTool("restart_listener"), {"listener_name": "LISTENER"}, _FakeCtx("t3")
    )
    assert result["status"] == "PENDING_APPROVAL"
    assert "t3" in g.pending_approvals


def test_tier3_approval_path_fails_loudly_with_no_os_execution_route():
    # restart_listener has no execution path from this process to the DB
    # host's OS (see db_tools.restart_listener's docstring) -- approving it
    # must fail loudly, never silently pretend to succeed. This is the same
    # honest limitation gcp_deploy/services/orchestrator/oracle_client.py
    # documents for the original reference implementation.
    g = Guardrails(AuditLog())
    g.before_tool(_FakeTool("restart_listener"), {"listener_name": "LISTENER"}, _FakeCtx("t3b"))
    pending = g.pending_approvals.pop("t3b")
    with pytest.raises(RuntimeError):
        db_tools.restart_listener(**pending["params"])


# ---------------------------------------------------------------------------
# Risk 2: false-positive anomalies -- unchanged (predict.py copied as-is)
# ---------------------------------------------------------------------------
def test_single_anomalous_reading_does_not_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"tick": 0, "active_blocked_sessions": 20,
               "cpu_utilization_pct": 90, "in_maintenance_window": False}
    assert detector.score(reading) is None


def test_consecutive_anomalous_readings_do_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"active_blocked_sessions": 20, "cpu_utilization_pct": 90,
               "in_maintenance_window": False}
    event = None
    for tick in range(config.CONSECUTIVE_ANOMALY_THRESHOLD):
        event = detector.score({**reading, "tick": tick})
    assert event is not None


def test_maintenance_window_reading_is_never_flagged():
    detector = AnomalyDetector(AuditLog())
    reading = {"active_blocked_sessions": 30, "cpu_utilization_pct": 95,
               "in_maintenance_window": True}
    for tick in range(5):
        assert detector.score({**reading, "tick": tick}) is None
def test_single_anomalous_runaway_query_does_not_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"tick": 0, "active_blocked_sessions": 0, "cpu_utilization_pct": 10,
               "max_query_duration_seconds": 120, "in_maintenance_window": False}
    assert detector.score(reading) is None
def test_consecutive_runaway_query_readings_do_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"active_blocked_sessions": 0, "cpu_utilization_pct": 10,
               "max_query_duration_seconds": 120, "in_maintenance_window": False}
    event = None
    for tick in range(config.CONSECUTIVE_ANOMALY_THRESHOLD):
        event = detector.score({**reading, "tick": tick})
    assert event is not None
    assert event["metric"] == "max_query_duration_seconds"
def test_consecutive_idle_in_transaction_readings_do_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"active_blocked_sessions": 0, "cpu_utilization_pct": 10,
               "idle_in_transaction_count": 5, "in_maintenance_window": False}
    event = None
    for tick in range(config.CONSECUTIVE_ANOMALY_THRESHOLD):
        event = detector.score({**reading, "tick": tick})
    assert event is not None
    assert event["metric"] == "idle_in_transaction_count"
def test_consecutive_connection_storm_readings_do_trigger_detection():
    detector = AnomalyDetector(AuditLog())
    reading = {"active_blocked_sessions": 0, "cpu_utilization_pct": 10,
               "connection_pct": 0.95, "in_maintenance_window": False}
    event = None
    for tick in range(config.CONSECUTIVE_ANOMALY_THRESHOLD):
        event = detector.score({**reading, "tick": tick})
    assert event is not None
    assert event["metric"] == "connection_pct"
def test_independent_signal_streaks_do_not_cross_contaminate():
    """A signal flapping in and out must not let a DIFFERENT signal
    piggyback its streak toward confirmation."""
    detector = AnomalyDetector(AuditLog())
    r1 = {"tick": 0, "active_blocked_sessions": 20, "cpu_utilization_pct": 10,
          "in_maintenance_window": False}
    r2 = {"tick": 1, "active_blocked_sessions": 0, "cpu_utilization_pct": 10,
          "max_query_duration_seconds": 120, "in_maintenance_window": False}
    assert detector.score(r1) is None  # blocked_sessions streak = 1
    assert detector.score(r2) is None  # blocked_sessions streak reset (dropped below
                                        # threshold); runaway_query streak = 1, not confirmed


# ---------------------------------------------------------------------------
# Risk 4: uncontrolled runbook / model drift -- unchanged (learn.py as-is)
# ---------------------------------------------------------------------------
def test_novel_runbook_goes_to_pending_review_not_live_store():
    store = RunbookStore()
    before = len(store.approved)
    learn.learn_from_incident(
        event={"incident_id": "t4", "query_text": "totally_novel_metric never_seen_before"},
        action_payload={"action_key": "kill_blocking_session"},
        healthy=True, runbook_store=store, audit_log=AuditLog(),
    )
    assert len(store.approved) == before
    assert len(store.pending_review) == 1


# ---------------------------------------------------------------------------
# Risk 5: multi-vendor coupling
# ---------------------------------------------------------------------------
def test_circuit_breaker_trips_after_repeated_failures():
    breaker = CircuitBreaker(name="test", failure_threshold=2, reset_seconds=60)

    def always_fails():
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(always_fails, now=0)

    with pytest.raises(CircuitOpenError):
        breaker.call(always_fails, now=1)


def test_open_db_breaker_short_circuits_before_tool_without_calling_toolbox():
    g = Guardrails(AuditLog())
    g.db_breaker.state = BreakerState.OPEN
    g.db_breaker._opened_at = time.time()  # just tripped -- still well within reset_seconds
    g.db_breaker._failure_count = g.db_breaker.failure_threshold
    result = g.before_tool(
        _FakeTool("kill_blocking_session"), {"sid": 1, "serial": 2}, _FakeCtx("t5", )
    )
    assert result["status"] == "BREAKER_OPEN"


def test_oracle_outage_is_raised_not_silently_swallowed():
    # pipeline.AdkOrchestrator.run_cycle() is what catches this and logs
    # breaker_tripped instead of crashing; sense.poll() itself (copied
    # unchanged) must propagate it faithfully regardless of what backend
    # (MCP Toolbox here, oracledb/a simulator originally) sits behind it.
    with pytest.raises(Exception):
        sense.poll(_FailingReadingSource())


# ---------------------------------------------------------------------------
# Risk 6: cost overrun -- unchanged (cost_guard.py as-is)
# ---------------------------------------------------------------------------
def test_cost_guard_blocks_calls_over_the_rate_cap():
    guard = CostGuard(max_calls_per_window=2, window_seconds=600, monthly_budget_usd=1000)
    assert guard.check_and_record(now=0) is True
    assert guard.check_and_record(now=1) is True
    assert guard.check_and_record(now=2) is False
    assert guard.alerts


def test_cost_guard_blocks_calls_over_the_monthly_budget():
    guard = CostGuard(max_calls_per_window=1000, window_seconds=600,
                       monthly_budget_usd=0.01, cost_per_call_usd=0.02)
    assert guard.check_and_record(now=0) is False


def test_before_model_falls_back_to_alert_only_when_cost_capped():
    g = Guardrails(AuditLog(), cost_guard=CostGuard(
        max_calls_per_window=1, window_seconds=600, monthly_budget_usd=1000
    ))
    assert g.before_model(_FakeCtx("t6"), None) is None       # 1st call allowed
    refusal = g.before_model(_FakeCtx("t6"), None)              # 2nd call capped
    assert refusal is not None
    assert refusal.turn_complete is True


# ---------------------------------------------------------------------------
# Email notifications: Tier 3 approval requests and post-resolution reports
# ---------------------------------------------------------------------------
def test_tier3_action_sends_an_approval_email():
    notifier = EmailNotifier()
    g = Guardrails(AuditLog(), notifier=notifier)
    g.before_tool(_FakeTool("restart_listener"), {"listener_name": "LISTENER"}, _FakeCtx("t7"))
    assert len(notifier.outbox) == 1
    assert notifier.outbox[0].to == config.APPROVAL_NOTIFY_EMAIL
    assert "Approval needed" in notifier.outbox[0].subject


def test_tier1_action_never_sends_an_approval_email():
    notifier = EmailNotifier()
    g = Guardrails(AuditLog(), notifier=notifier)
    g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 101, "serial": 5}, _FakeCtx("t8"))
    assert notifier.outbox == []


def test_status_report_email_sent_on_resolution():
    store = RunbookStore()
    notifier = EmailNotifier()
    learn.learn_from_incident(
        event={"incident_id": "t9", "query_text": "active_blocked_sessions sustained_high"},
        action_payload={"action_key": "kill_blocking_session", "params": {"sid": 1, "serial": 2}},
        healthy=True, runbook_store=store, audit_log=AuditLog(), notifier=notifier,
    )
    assert len(notifier.outbox) == 1
    assert notifier.outbox[0].to == config.STATUS_REPORT_EMAIL
    assert "RESOLVED" in notifier.outbox[0].subject


def test_status_report_email_flags_unhealthy_outcome():
    store = RunbookStore()
    notifier = EmailNotifier()
    learn.learn_from_incident(
        event={"incident_id": "t10", "query_text": "active_blocked_sessions sustained_high"},
        action_payload={"action_key": "kill_blocking_session", "params": {"sid": 1, "serial": 2}},
        healthy=False, runbook_store=store, audit_log=AuditLog(), notifier=notifier,
    )
    assert len(notifier.outbox) == 1
    assert "UNHEALTHY" in notifier.outbox[0].subject


# ---------------------------------------------------------------------------
# Oracle regression coverage for this session's two guardrail_callbacks.py
# fixes (cross-engine guard + false action_executed on short-circuit).
# Existing Tier 1/3 Oracle tests above never set ctx.state["engine"], so
# incident_engine resolved to None and the cross-engine check in before_tool()
# was silently skipped for every one of them -- none actually proved a
# genuine engine="oracle" incident survives the guard cleanly. A live staged
# memory-pressure rehearsal for increase_pga_target was ruled out: executor_sa
# (the account poll_telemetry actually connects as) has no catalog privileges
# to read v$pgastat, and the VM has only ~1.4GB RAM available -- so this is
# deterministic coverage of the same code path instead.
# ---------------------------------------------------------------------------
def test_oracle_engine_tagged_tier3_call_is_not_blocked_by_the_cross_engine_guard():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("oracle-incident-2000")
    ctx.state["engine"] = "oracle"
    result = g.before_tool(_FakeTool("increase_pga_target"), {"target_mb": 400}, ctx)
    assert result["status"] == "PENDING_APPROVAL"
    assert "oracle-incident-2000" in g.pending_approvals


def test_oracle_short_circuited_tier3_call_does_not_log_a_false_action_executed():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("oracle-incident-2001")
    ctx.state["engine"] = "oracle"
    result = g.before_tool(_FakeTool("increase_pga_target"), {"target_mb": 400}, ctx)
    assert result["status"] == "PENDING_APPROVAL"
    assert "oracle-incident-2001" not in g._in_flight_snapshots
    g.after_tool(_FakeTool("increase_pga_target"), {"target_mb": 400}, ctx, result)
    executed = [e for e in g.audit_log.events() if e.event_type == "action_executed"]
    assert executed == []
    assert "oracle-incident-2001" in g.pending_approvals


def test_oracle_engine_tagged_genuine_call_logs_action_executed_correctly():
    g = Guardrails(AuditLog())
    ctx = _FakeCtx("oracle-incident-2002")
    ctx.state["engine"] = "oracle"
    result = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 1, "serial": 1}, ctx)
    assert result is None
    assert "oracle-incident-2002" in g._in_flight_snapshots
    g.after_tool(_FakeTool("kill_blocking_session"), {"sid": 1, "serial": 1}, ctx, {"result": "ok"})
    executed = [e for e in g.audit_log.events() if e.event_type == "action_executed"]
    assert len(executed) == 1
    assert executed[0].detail["incident_id"] == "oracle-incident-2002"
    assert "oracle-incident-2002" not in g._in_flight_snapshots
