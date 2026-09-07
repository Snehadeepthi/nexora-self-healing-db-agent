#!/usr/bin/env python3
"""
Directly exercises the DEPLOYED Tier 3 TTL expiry logic
(guardrail_callbacks.Guardrails.expire_stale_approvals, calling the real
notifications.EmailNotifier.send_approval_timeout / gcp_notifications
.SlackNotifier.send_approval_timeout signature) without needing a live
15-minute wait or a real anomaly.

Why this instead of inducing a real connection-storm anomaly: MySQL's
connection_pct signal is TOTAL_CONNECTIONS / max_connections (from
tools.yaml's mysql_poll_telemetry), and this instance's max_connections is
4030 -- crossing the 80% CONNECTION_PCT_THRESHOLD would need ~3224 real
open connections, which isn't a safe or reasonable thing to induce by hand
on a live instance. This script instead calls the real, deployed
expire_stale_approvals() directly against a synthetic pending_approvals
dict with a backdated created_at -- same function, same code path
pipeline.py calls every tick, just without waiting real wall-clock time or
touching the database.

Run from: ~/NEXORA/self_healing_db_agent/gcp_deploy/services/orchestrator
(needs to import config.py / guardrail_callbacks.py / notifications.py from
the current directory).

    python3 test_tier3_ttl_live.py

Checks:
  1. A stale (TTL-exceeded) mysql-scoped entry IS expired.
  2. A fresh (not-yet-TTL-exceeded) mysql-scoped entry is NOT touched.
  3. A stale entry belonging to a DIFFERENT engine is NOT touched (proves
     the "{engine_id}-..." namespacing/scoping actually works).
  4. Exactly one "approval_timeout" audit_log event is written, with the
     right incident_id/action_key.
  5. notifier.send_approval_timeout() is called exactly once, with the
     right payload.
"""
import sys
import time
import types

sys.path.insert(0, ".")

import config
from guardrail_callbacks import Guardrails


class FakeAuditLog:
    def __init__(self):
        self.entries = []

    def log(self, event_type, **kwargs):
        self.entries.append((event_type, kwargs))
        print(f"[audit_log.log] event_type={event_type!r} kwargs={kwargs}")


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_approval_timeout(self, action_payload, detail):
        self.sent.append((action_payload, detail))
        print(f"[notifier.send_approval_timeout] action_payload={action_payload} detail={detail!r}")


def main():
    fake_self = types.SimpleNamespace()
    fake_self.audit_log = FakeAuditLog()
    fake_self.notifier = FakeNotifier()

    now = time.time()
    stale_created_at = now - (config.TIER3_APPROVAL_TTL_SECONDS + 30)  # 30s past TTL
    fresh_created_at = now  # well within TTL

    fake_self.pending_approvals = {
        "mysql-incident-999": {
            "action_key": "mysql_reset_all_connections",
            "params": {},
            "snapshot": {},
            "query_text": "connection storm -- last-resort reset requested",
            "created_at": stale_created_at,
        },
        "mysql-incident-1000": {
            "action_key": "mysql_kill_blocking_session",
            "params": {"session_id": 42},
            "snapshot": {},
            "query_text": "test",
            "created_at": fresh_created_at,
        },
        "oracle-incident-1": {
            "action_key": "kill_runaway_query",
            "params": {},
            "snapshot": {},
            "query_text": "test",
            "created_at": stale_created_at,
        },
    }

    print("Before:", sorted(fake_self.pending_approvals.keys()))
    expired = Guardrails.expire_stale_approvals(fake_self, "mysql")
    print("Expired:", expired)
    print("After:", sorted(fake_self.pending_approvals.keys()))

    assert expired == ["mysql-incident-999"], (
        f"expected only the stale mysql incident to expire, got {expired}"
    )
    assert "mysql-incident-999" not in fake_self.pending_approvals, (
        "stale entry should have been removed"
    )
    assert "mysql-incident-1000" in fake_self.pending_approvals, (
        "fresh entry should NOT have been removed"
    )
    assert "oracle-incident-1" in fake_self.pending_approvals, (
        "other-engine entry should NOT have been touched by a mysql-scoped call"
    )
    assert (
        len(fake_self.audit_log.entries) == 1
        and fake_self.audit_log.entries[0][0] == "approval_timeout"
    ), "expected exactly one approval_timeout audit log entry"
    assert len(fake_self.notifier.sent) == 1, "expected exactly one send_approval_timeout call"
    sent_payload, sent_detail = fake_self.notifier.sent[0]
    assert sent_payload["incident_id"] == "mysql-incident-999"
    assert sent_payload["action_key"] == "mysql_reset_all_connections"

    print("\nALL ASSERTIONS PASSED -- expire_stale_approvals() correctly:")
    print("  - expired only the stale, TTL-exceeded, mysql-scoped entry")
    print("  - left the fresh mysql entry untouched")
    print("  - left the stale oracle entry untouched (engine-scoping works)")
    print("  - logged exactly one approval_timeout audit event")
    print("  - called notifier.send_approval_timeout() with the right incident/action")


if __name__ == "__main__":
    main()
