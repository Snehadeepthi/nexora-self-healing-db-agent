# Self-Healing DB Agent — Reference Implementation + Risk Mitigations

Runnable, local reference implementation of the Sense → Predict → Reason →
Act → Learn pipeline from the *GCP Implementation Guide: Autonomous
Self-Healing Database Reliability Platform*, with the eight risks from the
companion risk register implemented as working code rather than just
documented as prose.

No GCP project, Oracle instance, or Vertex AI key is required — everything
runs against local simulators. Each module's docstring says exactly which
step of the implementation guide it corresponds to and which real GCP
resource it stands in for.

## Quick start

```bash
pip install -r requirements.txt
python demo.py              # runs 8 narrated end-to-end scenarios
pytest test_safety.py -v    # the CI safety gate — same suite cloudbuild.yaml runs
```

## File map

| File | Pipeline stage | Production GCP equivalent |
|---|---|---|
| `config.py` | policy | allowlist definition, tiers, thresholds |
| `simulators/oracle_simulator.py` | — | Oracle Database@Google Cloud (Step 2) |
| `sense.py` | Sense | cloud_functions/poller/main.py (Step 3) |
| `predict.py` | Predict | BigQuery ML ARIMA_PLUS + ML.DETECT_ANOMALIES (Step 4) |
| `runbooks.py`, `llm_client.py`, `reason.py` | Reason | Cloud Run reasoner + Vertex AI Gemini + Oracle 23ai VECTOR (Step 5) |
| `act.py` | Act | Cloud Run guarded-executor (Step 6) |
| `notifications.py` | Act / Learn | email channel alongside the Step 6 Slack webhook |
| `learn.py` | Learn | BigQuery audit_log + runbook insert (Step 7) |
| `orchestrator.py` | all | the end-to-end wiring (Step 8) |
| `test_safety.py` | — | the Cloud Build deploy gate (Step 9) |

## Email notifications

Two moments in the loop now send email via `notifications.EmailNotifier`:

1. **Tier 3 approval request** — the instant `act.py` gates a Tier 3 action,
   it emails `config.APPROVAL_NOTIFY_EMAIL` with the incident, the proposed
   action, and its parameters. A human approves via
   `executor.approve(incident_id)` or `orchestrator.approve_and_resolve(incident_id)`.
2. **Post-resolution status report** — once `learn.py` receives the post-fix
   health check result (healthy or not), it emails
   `config.STATUS_REPORT_EMAIL` a summary with MTTD/MTTR pulled from
   `audit.py`. This fires for both healthy and unhealthy outcomes.

`EmailNotifier._send()` records messages in `self.outbox` instead of sending
real mail, so the reference pipeline needs no SMTP credentials — see that
method's docstring for the `smtplib` snippet to drop in for production.

## Risk → code map

Each risk from the risk register is implemented, not just described:

| # | Risk | Where it's mitigated |
|---|---|---|
| 1 | Autonomous action on wrong diagnosis | `act.py` (Tier 3 never auto-executes, pre-action snapshot) + `llm_client.py` (hallucination firewall) |
| 2 | False-positive anomalies | `predict.py` (requires `config.CONSECUTIVE_ANOMALY_THRESHOLD` consecutive readings) + `sense.py` (`config.MAINTENANCE_WINDOWS` suppression) |
| 3 | Latency in the closed loop | `audit.py` (`mttd_seconds` / `mttr_seconds`, computed straight from the audit trail) |
| 4 | Uncontrolled runbook / model drift | `runbooks.py` + `learn.py` (`pending_review` queue, `approve_pending_runbook()` requires a human) |
| 5 | Multi-vendor coupling | `circuit_breaker.py` (wraps every Oracle and Vertex AI call; trips to alert-only mode) |
| 6 | Cost overrun at scale | `cost_guard.py` (rolling rate cap + monthly budget cap in front of every Gemini call) |
| 7 | Allowlist creep / IAM erosion | `allowlist_governor.py` (`SIGNOFF_LEDGER`; `config.get_action()` refuses anything unsigned) + `test_safety.py` (CI gate) |
| 8 | Compliance / audit exposure | `audit.py` (`compliance_summary()`, the exportable audit trail) + `notifications.py` (status-report emails as a human-readable audit artifact) |

## Going to production

Each module's docstring notes exactly what to swap:

- `simulators/oracle_simulator.py` → a real `oracledb` connection (Step 2/3)
- `llm_client.py`'s `_propose_action_key()` → a real
  `GenerativeModel("gemini-2.5-flash").generate_content(prompt)` call (Step 5);
  the validation logic around it does **not** need to change
- `runbooks.py` → Oracle 23ai `VECTOR` columns + `text-embedding-005` instead
  of the hand-rolled bag-of-words index
- `allowlist_governor.SIGNOFF_LEDGER` → a persisted table or a
  `allowlist_signoffs.json` reviewed via pull request, instead of an
  in-memory dict
- `audit.py` → the BigQuery `db_ops.audit_log` table described in Step 9,
  instead of an in-memory list

## Notes

- `_bootstrap_reference_signoffs()` in `allowlist_governor.py` auto-signs the
  shipped allowlist purely so the demo runs out of the box. **Delete this in
  a real deployment** — signoffs should only ever come from an actual
  security/DBA reviewer.
- The reference demo assumes every executed fix is "healthy" afterward
  (`orchestrator.py`); production should re-poll telemetry before calling
  `learn.py`, exactly as `learn.py`'s `healthy` parameter implies.
