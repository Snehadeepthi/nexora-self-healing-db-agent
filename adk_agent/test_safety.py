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
