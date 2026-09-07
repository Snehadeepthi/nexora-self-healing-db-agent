"""
demo.py
Runnable, end-to-end walkthrough of the pipeline PLUS every risk mitigation
in the risk register. No GCP/Oracle/Vertex AI credentials required -- this
runs entirely against the local simulators.

    python demo.py

Each section below is labeled with the risk it demonstrates.
"""

from act import GuardedExecutionEngine
from allowlist_governor import SIGNOFF_LEDGER, audit_unsigned_actions
from audit import AuditLog
from cost_guard import CostGuard
from notifications import EmailNotifier
from orchestrator import Orchestrator
from simulators.oracle_simulator import DbExecutor, OracleSimulator


def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def scenario_normal_incident():
    banner("SCENARIO 1 -- Genuine incident, Tier 1 action, auto-resolved end-to-end")
    oracle = OracleSimulator()
    oracle.schedule_incident(tick=2, blocked_sessions=18, duration=5)
    orch = Orchestrator(oracle, DbExecutor())

    for _ in range(10):
        trace = orch.run_cycle()
        print(f"  tick result: {trace['stage']:8s} -> {trace['outcome']}")
        if trace["outcome"] == "EXECUTED":
            break

    print("\nAudit trail for this run:")
    incident_id = None
    for e in orch.audit_log.events():
        iid = e.detail.get("incident_id", "")
        incident_id = incident_id or iid
        print(f"  [{e.event_type}] {iid}")

    if incident_id:
        print(f"\nMTTD: {orch.audit_log.mttd_seconds(incident_id)}s "
              f"(near-zero here since ticks run back-to-back in the demo;\n"
              f"      in production this is real wall-clock time between onset and confirmation)")

    print("\nStatus report email(s) sent once resolution was confirmed:")
    for email in orch.notifier.outbox:
        print(f"  -> {email.to}: {email.subject}")


def scenario_maintenance_window_suppressed():
    banner("SCENARIO 2 (Risk 2) -- Maintenance-window spike never triggers an action")
    import predict

    audit_log = AuditLog()
    detector = predict.AnomalyDetector(audit_log)
    for tick in range(5):
        reading = {"tick": tick, "active_blocked_sessions": 25,
                   "cpu_utilization_pct": 95, "in_maintenance_window": True}
        event = detector.score(reading)
        print(f"  tick {tick}: blocked=25, in_maintenance_window=True -> event={event}")
    print("Result: no action ever proposed, despite a large, sustained spike.")


def scenario_tier3_requires_approval():
    banner("SCENARIO 3 (Risk 1) -- Tier 3 action waits for human approval, notified by email")
    audit_log = AuditLog()
    notifier = EmailNotifier()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log, notifier=notifier)
    result = engine.execute({
        "action_key": "restart_listener",
        "params": {"listener_name": "LISTENER"},
        "incident_id": "demo-tier3",
    })
    print(f"  immediate result: {result.status} (never auto-executed)")
    last_email = notifier.outbox[-1]
    print(f"  approval email -> {last_email.to}")
    print(f"    subject: {last_email.subject}")
    approved = engine.approve("demo-tier3")
    print(f"  after human approval:  {approved.status} -- {approved.detail}")


def scenario_allowlist_signoff_enforced():
    banner("SCENARIO 4 (Risk 7) -- Executor refuses an action with no recorded signoff")
    saved = SIGNOFF_LEDGER.pop("kill_blocking_session")
    audit_log = AuditLog()
    engine = GuardedExecutionEngine(DbExecutor(), audit_log)
    result = engine.execute({
        "action_key": "kill_blocking_session",
        "params": {"sid": 101, "serial": 5},
        "incident_id": "demo-unsigned",
    })
    print(f"  result: {result.status} -- {result.detail}")
    SIGNOFF_LEDGER["kill_blocking_session"] = saved  # restore for later scenarios
    print(f"  unsigned actions remaining in ALLOWLIST: {audit_unsigned_actions()}")


def scenario_circuit_breaker_trips():
    banner("SCENARIO 5 (Risk 5) -- Oracle outage trips the breaker; pipeline degrades gracefully")
    oracle = OracleSimulator()
    oracle.schedule_outage(tick=0, duration=5)
    orch = Orchestrator(oracle, DbExecutor())
    for _ in range(4):
        trace = orch.run_cycle()
        print(f"  tick result: {trace['stage']:8s} -> {trace['outcome']}")


def scenario_cost_guard_caps_spend():
    banner("SCENARIO 6 (Risk 6) -- Cost guard caps Gemini calls during a burst of anomalies")
    guard = CostGuard(max_calls_per_window=3, window_seconds=600, monthly_budget_usd=1000)
    for i in range(6):
        allowed = guard.check_and_record(now=i)
        print(f"  call {i}: allowed={allowed}")
    print(f"  alerts raised: {guard.alerts}")
    print(f"  month spend so far: ${guard.month_spend_usd}")


def scenario_novel_runbook_review_queue():
    banner("SCENARIO 7 (Risk 4) -- Novel incident lands in review, not the live store")
    import learn
    from runbooks import RunbookStore

    audit_log = AuditLog()
    store = RunbookStore()
    print(f"  approved runbooks before: {len(store.approved)}")
    learn.learn_from_incident(
        event={"incident_id": "demo-novel", "query_text": "brand_new_wait_event never_seen"},
        action_payload={"action_key": "kill_blocking_session"},
        healthy=True, runbook_store=store, audit_log=audit_log,
    )
    print(f"  approved runbooks after:  {len(store.approved)}  (unchanged)")
    print(f"  pending review:           {len(store.pending_review)}")
    approved = store.approve_pending_runbook(store.pending_review[0].title, reviewer="oncall-lead")
    print(f"  after human review, promoted to live store: '{approved.title}'")


def scenario_tier3_email_approval_end_to_end():
    banner("SCENARIO 9 -- Tier 3 end-to-end: approval email -> human approves -> status-report email")
    oracle = OracleSimulator()
    orch = Orchestrator(oracle, DbExecutor())

    # Force a Tier 3 action directly through Act, as if Reason had proposed
    # it (skips Sense/Predict here purely so the demo doesn't need to wait
    # for a listener-outage runbook match -- act.py's gating logic is
    # identical either way).
    action_payload = {
        "action_key": "restart_listener",
        "params": {"listener_name": "LISTENER"},
        "incident_id": "demo-tier3-e2e",
        "query_text": "listener unreachable connection refused",
        "issue": "listener unreachable connection refused",
    }
    result = orch.executor.execute(action_payload)
    print(f"  1. immediate result: {result.status}")
    approval_email = orch.notifier.outbox[-1]
    print(f"     approval email -> {approval_email.to}: {approval_email.subject}")

    final = orch.approve_and_resolve("demo-tier3-e2e")
    print(f"  2. after human approval: {final.status}")
    status_email = orch.notifier.outbox[-1]
    print(f"     status report email -> {status_email.to}: {status_email.subject}")
    print(f"\n  total emails sent this scenario: {len(orch.notifier.outbox)}")


def scenario_compliance_summary():
    banner("SCENARIO 8 (Risk 8) -- Compliance summary derived from the audit trail")

    oracle = OracleSimulator()
    oracle.schedule_incident(tick=1, blocked_sessions=18, duration=5)
    orch = Orchestrator(oracle, DbExecutor())
    for _ in range(8):
        orch.run_cycle()
    summary = orch.audit_log.compliance_summary()
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("  (this dict is what would be exported to Looker Studio / a compliance review)")


if __name__ == "__main__":
    scenario_normal_incident()
    scenario_maintenance_window_suppressed()
    scenario_tier3_requires_approval()
    scenario_allowlist_signoff_enforced()
    scenario_circuit_breaker_trips()
    scenario_cost_guard_caps_spend()
    scenario_novel_runbook_review_queue()
    scenario_tier3_email_approval_end_to_end()
    scenario_compliance_summary()
    banner("Done. Run `pytest test_safety.py -v` to see the CI safety gate.")
