"""
test_suppression_autoclear.py

Unit tests for Guardrails.record_tick_health() (see
apply_suppression_autoclear.py). Drop this file into
gcp_deploy/services/orchestrator/ alongside guardrail_callbacks.py and run:

    pytest test_suppression_autoclear.py -v

Pure unit tests against Guardrails directly -- no Toolbox connection, no
ADK Runner, no real audit backend needed, same dependency-injection shape
test_safety.py already relies on (Guardrails(audit_log, notifier=...)
accepts anything with the right shape).
"""
import config
from guardrail_callbacks import Guardrails


class _FakeAuditLog:
    def __init__(self):
        self.events = []

    def log(self, event_type, **kwargs):
        self.events.append({"event_type": event_type, **kwargs})


class _FakeNotifier:
    """Guardrails.__init__ only imports notifications.EmailNotifier when
    notifier is None -- passing this stub avoids that import (and its
    real-SMTP-config requirements) entirely, same reasoning
    apply_tier3_ttl_fix.py's own test coverage uses."""

    def send_approval_request(self, *a, **k):
        raise AssertionError("not expected to be called by these tests")

    def send_approval_timeout(self, *a, **k):
        raise AssertionError("not expected to be called by these tests")


def _guardrails():
    return Guardrails(_FakeAuditLog(), notifier=_FakeNotifier())


def _suppress(g, engine_id, action_key, since_incident_id="inc-0", repeat_count=2):
    """Directly arms a suppression entry, bypassing expire_stale_approvals
    (which needs a real pending_approvals + TTL elapsed) -- these tests are
    about record_tick_health()'s own logic, not how something got
    suppressed in the first place."""
    g.suppressed[(engine_id, action_key)] = {
        "since": 0.0, "since_incident_id": since_incident_id,
        "repeat_count": repeat_count, "last_incident_id": since_incident_id,
    }


def test_streak_never_reaches_threshold_while_anomalies_keep_firing():
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")
    for _ in range(10):
        cleared = g.record_tick_health("oracle", anomaly_detected=True)
        assert cleared == []
    assert ("oracle", "flush_shared_pool") in g.suppressed


def test_autoclears_after_threshold_consecutive_healthy_ticks():
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")
    for i in range(config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS - 1):
        cleared = g.record_tick_health("oracle", anomaly_detected=False)
        assert cleared == [], f"cleared too early on healthy tick {i + 1}"
        assert ("oracle", "flush_shared_pool") in g.suppressed

    cleared = g.record_tick_health("oracle", anomaly_detected=False)
    assert cleared == [("oracle", "flush_shared_pool")]
    assert ("oracle", "flush_shared_pool") not in g.suppressed

    auto_clear_events = [e for e in g.audit_log.events if e["event_type"] == "action_suppression_auto_cleared"]
    assert len(auto_clear_events) == 1
    assert auto_clear_events[0]["action_key"] == "flush_shared_pool"


def test_anomaly_resets_streak_before_threshold_is_reached():
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")

    g.record_tick_health("oracle", anomaly_detected=False)
    g.record_tick_health("oracle", anomaly_detected=False)
    # one short of the threshold -- a fresh anomaly here must reset, not just pause, the streak
    cleared = g.record_tick_health("oracle", anomaly_detected=True)
    assert cleared == []
    assert ("oracle", "flush_shared_pool") in g.suppressed

    # two more healthy ticks (not three) should NOT clear -- the streak restarted at the anomaly above
    g.record_tick_health("oracle", anomaly_detected=False)
    cleared = g.record_tick_health("oracle", anomaly_detected=False)
    assert cleared == []
    assert ("oracle", "flush_shared_pool") in g.suppressed

    # the third consecutive healthy tick since the reset finally clears it
    cleared = g.record_tick_health("oracle", anomaly_detected=False)
    assert cleared == [("oracle", "flush_shared_pool")]


def test_autoclear_is_scoped_to_the_reporting_engine_only():
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")
    _suppress(g, "alloydb", "alloydb_terminate_idle_in_transaction")

    for _ in range(config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS):
        g.record_tick_health("oracle", anomaly_detected=False)

    assert ("oracle", "flush_shared_pool") not in g.suppressed
    assert ("alloydb", "alloydb_terminate_idle_in_transaction") in g.suppressed


def test_multiple_suppressed_actions_on_the_same_engine_all_clear_together():
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")
    _suppress(g, "oracle", "kill_blocking_session")

    for _ in range(config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS - 1):
        g.record_tick_health("oracle", anomaly_detected=False)
    cleared = g.record_tick_health("oracle", anomaly_detected=False)

    assert set(cleared) == {("oracle", "flush_shared_pool"), ("oracle", "kill_blocking_session")}
    assert g.suppressed == {}


def test_healthy_ticks_with_nothing_suppressed_are_a_harmless_noop():
    g = _guardrails()
    for _ in range(config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS + 2):
        cleared = g.record_tick_health("oracle", anomaly_detected=False)
        assert cleared == []
    assert g.suppressed == {}
    assert g.audit_log.events == []


def test_manual_clear_and_autoclear_do_not_double_log_or_error():
    """clear_suppression() (operator path) popping an entry the streak
    hasn't reached yet must leave record_tick_health() to find nothing left
    to clear -- no KeyError, no duplicate audit entry."""
    g = _guardrails()
    _suppress(g, "oracle", "flush_shared_pool")

    g.record_tick_health("oracle", anomaly_detected=False)  # 1 of 3
    g.clear_suppression("oracle", "flush_shared_pool")      # operator beats the auto-clear to it

    for _ in range(config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS):
        cleared = g.record_tick_health("oracle", anomaly_detected=False)
        assert cleared == []

    auto_clear_events = [e for e in g.audit_log.events if e["event_type"] == "action_suppression_auto_cleared"]
    assert auto_clear_events == []
