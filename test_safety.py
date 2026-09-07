"""
test_safety.py
The CI safety gate (Step 9 of the implementation guide) -- cloudbuild.yaml
runs `pytest test_safety.py -v` before any deploy step, and a single failure
here blocks the rollout. Each test below maps to one risk from the risk
register.

Run: pytest test_safety.py -v
"""

import pytest

import config
from allowlist_governor import SIGNOFF_LEDGER, audit_unsigned_actions
from act import GuardedExecutionEngine
from audit import AuditLog
from circuit_breaker import CircuitBreaker, CircuitOpenError
from cost_guard import CostGuard
from notifications import EmailNotifier
from predict import AnomalyDetector
from runbooks import RunbookStore
from simulators.oracle_simulator import DbExecutor, OracleSimulator
import sense


# ---------------------------------------------------------------------------
# Risk 7: allowlist creep / IAM erosion
# ---------------------------------------------------------------------------
def test_every_allowlist_entry_has_a_valid_signoff():
    unsigned = audit_unsigned_actions()
    assert unsigned == [], f"Unsigned allowlist entries block deploy: {unsigned}"


def test_action_without_signoff_is_rejected_by_executor():
    # Temporarily revoke a signoff to prove act.py actually enforces it,
    # not just that the ledger happens to be populated.
    saved = SIGNOFF_LEDGER.pop("flush_shared_pool")
    try:
        audit_log = AuditLog()
        engine = GuardedExecutionEngine(DbExecutor(), audit_log)
        result = engine.execute(
            {"action_key": "flush_shared_pool", "params": {}, "incident_id": "t1"}
        )
        assert result.status == "REJECTED"
    finally:
        SIGNOFF_LEDGER["flush_shared_pool"] = saved


# ---------------------------------------------------------------------------
# Risk 1: autonomous action on wrong diagnosis (hallucination firewall + tiering)
# ---------------------------------------------------------------------------
def test_hallucinated_action_is_rejected_before_execution():
    from llm_client import HallucinatedActionError, generate_action

    with pytest.raises(HallucinatedActionError):
        generate_action({"query_text": "unknown"}, runbook=None)  # -> "UNKNOWN" action_key


def test_tier3_action_never_auto_executes():
    audit_log = AuditLog()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log)
    result = engine.execute({
        "action_key": "restart_listener",
        "params": {"listener_name": "LISTENER"},
        "incident_id": "t2",
    })
    assert result.status == "PENDING_APPROVAL"
    assert "t2" in engine.pending_approvals


def test_tier3_action_executes_only_after_explicit_approval():
    audit_log = AuditLog()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log)
    engine.execute({
        "action_key": "restart_listener",
        "params": {"listener_name": "LISTENER"},
        "incident_id": "t3",
    })
    result = engine.approve("t3")
    assert result.status == "EXECUTED"


# ---------------------------------------------------------------------------
# Risk 2: false-positive anomalies
# ---------------------------------------------------------------------------
def test_single_anomalous_reading_does_not_trigger_detection():
    audit_log = AuditLog()
    detector = AnomalyDetector(audit_log)
    reading = {"tick": 0, "active_blocked_sessions": 20,
               "cpu_utilization_pct": 90, "in_maintenance_window": False}
    assert detector.score(reading) is None


def test_consecutive_anomalous_readings_do_trigger_detection():
    audit_log = AuditLog()
    detector = AnomalyDetector(audit_log)
    reading = {"active_blocked_sessions": 20, "cpu_utilization_pct": 90,
               "in_maintenance_window": False}
    event = None
    for tick in range(config.CONSECUTIVE_ANOMALY_THRESHOLD):
        event = detector.score({**reading, "tick": tick})
    assert event is not None


def test_maintenance_window_reading_is_never_flagged():
    audit_log = AuditLog()
    detector = AnomalyDetector(audit_log)
    reading = {"active_blocked_sessions": 30, "cpu_utilization_pct": 95,
               "in_maintenance_window": True}
    for tick in range(5):
        event = detector.score({**reading, "tick": tick})
        assert event is None


# ---------------------------------------------------------------------------
# Risk 4: uncontrolled runbook / model drift
# ---------------------------------------------------------------------------
def test_novel_runbook_goes_to_pending_review_not_live_store():
    import learn

    audit_log = AuditLog()
    store = RunbookStore()
    before = len(store.approved)
    learn.learn_from_incident(
        event={"incident_id": "t4", "query_text": "totally_novel_metric never_seen_before"},
        action_payload={"action_key": "kill_blocking_session"},
        healthy=True, runbook_store=store, audit_log=audit_log,
    )
    assert len(store.approved) == before   # never auto-added to the live store
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


def test_oracle_outage_is_raised_not_silently_swallowed():
    # orchestrator.run_cycle() is what catches this and logs breaker_tripped
    # instead of crashing; sense.poll() itself must propagate it faithfully.
    oracle = OracleSimulator()
    oracle.schedule_outage(tick=0, duration=1)
    with pytest.raises(Exception):
        sense.poll(oracle)


# ---------------------------------------------------------------------------
# Risk 6: cost overrun
# ---------------------------------------------------------------------------
def test_cost_guard_blocks_calls_over_the_rate_cap():
    guard = CostGuard(max_calls_per_window=2, window_seconds=600, monthly_budget_usd=1000)
    assert guard.check_and_record(now=0) is True
    assert guard.check_and_record(now=1) is True
    assert guard.check_and_record(now=2) is False  # 3rd call in-window is capped
    assert guard.alerts


def test_cost_guard_blocks_calls_over_the_monthly_budget():
    guard = CostGuard(max_calls_per_window=1000, window_seconds=600,
                       monthly_budget_usd=0.01, cost_per_call_usd=0.02)
    assert guard.check_and_record(now=0) is False


# ---------------------------------------------------------------------------
# Email notifications: Tier 3 approval requests and post-resolution reports
# ---------------------------------------------------------------------------
def test_tier3_action_sends_an_approval_email():
    audit_log = AuditLog()
    notifier = EmailNotifier()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log, notifier=notifier)
    engine.execute({
        "action_key": "restart_listener",
        "params": {"listener_name": "LISTENER"},
        "incident_id": "t5",
    })
    assert len(notifier.outbox) == 1
    assert notifier.outbox[0].to == config.APPROVAL_NOTIFY_EMAIL
    assert "Approval needed" in notifier.outbox[0].subject


def test_tier1_action_never_sends_an_approval_email():
    audit_log = AuditLog()
    notifier = EmailNotifier()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log, notifier=notifier)
    engine.execute({
        "action_key": "kill_blocking_session",
        "params": {"sid": 101, "serial": 5},
        "incident_id": "t6",
    })
    assert notifier.outbox == []  # Tier 1 auto-executes; nothing to approve


def test_status_report_email_sent_on_resolution():
    import learn

    audit_log = AuditLog()
    store = RunbookStore()
    notifier = EmailNotifier()
    learn.learn_from_incident(
        event={"incident_id": "t7", "query_text": "active_blocked_sessions sustained_high"},
        action_payload={"action_key": "kill_blocking_session", "params": {"sid": 1, "serial": 2}},
        healthy=True, runbook_store=store, audit_log=audit_log, notifier=notifier,
    )
    assert len(notifier.outbox) == 1
    assert notifier.outbox[0].to == config.STATUS_REPORT_EMAIL
    assert "RESOLVED" in notifier.outbox[0].subject


def test_status_report_email_flags_unhealthy_outcome():
    import learn

    audit_log = AuditLog()
    store = RunbookStore()
    notifier = EmailNotifier()
    learn.learn_from_incident(
        event={"incident_id": "t8", "query_text": "active_blocked_sessions sustained_high"},
        action_payload={"action_key": "kill_blocking_session", "params": {"sid": 1, "serial": 2}},
        healthy=False, runbook_store=store, audit_log=audit_log, notifier=notifier,
    )
    assert len(notifier.outbox) == 1
    assert "UNHEALTHY" in notifier.outbox[0].subject
