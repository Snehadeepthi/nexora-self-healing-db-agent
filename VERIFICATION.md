# Verification

This document exists so a claim in the README, the case study, or the pitch
deck is never taken on faith. Every number below was produced by actually
running the command shown, against either the real test suite or the live,
running production system — not copied from an earlier draft.

## 1. Test suite — real, current pass counts

NEXORA ships two independent test suites: the local zero-dependency
reference implementation at the repo root, and the production orchestrator
under `gcp_deploy/services/orchestrator/`. Both were run fresh on
2026-09-07.

**Local reference implementation** (`test_safety.py`, repo root):

```bash
cd ~/NEXORA/self_healing_db_agent
source nexora_venv/bin/activate
pytest test_safety.py -v
```

```
17 passed in 0.06s
```

**Production orchestrator** (`gcp_deploy/services/orchestrator/`):

```bash
pytest gcp_deploy/services/orchestrator/test_safety.py \
       gcp_deploy/services/orchestrator/test_suppression_autoclear.py -v
```

```
43 passed, 2 warnings in 4.71s
```

**Total: 60 tests passing, 0 failing, across both suites.** The 2 warnings
are an unrelated `aiohttp` unclosed-session notice and one upstream ADK
deprecation notice from `google-adk` itself — neither originates in NEXORA's
own code, and neither affects the pass/fail result.

These two suites cover different things and both matter: the root suite
exercises the reasoning/guardrail logic in isolation against local
simulators (fast, no GCP project required — see `REPRODUCE.md`); the
`gcp_deploy` suite exercises the same guardrail logic as it's actually wired
into the deployed orchestrator, including the Tier 3 approval-email path,
the cost guard, the circuit breaker, the cross-engine short-circuit guard,
and the suppression auto-clear state machine.

## 2. Live system — real, current responses

The production system is live at
**https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/** — nothing
below requires deploying anything; it queries the actual running service.

**Compliance / lifetime action counts:**

```bash
curl -s https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/compliance
```

A real response, captured 2026-09-07:

```json
{
  "breaker_trips": 257,
  "hallucinated_actions_rejected": 0,
  "tier3_actions_pending_or_approved": 119,
  "total_actions_executed": 100,
  "unsigned_actions_blocked": 0
}
```

`hallucinated_actions_rejected: 0` and `unsigned_actions_blocked: 0` aren't
evidence those guardrails are untested — they're evidence they've never had
to fire in production, because nothing has ever reached the agent without a
valid signoff or outside the allowlist in the first place. `test_safety.py`
is where those specific rejection paths are exercised directly (see
`test_hallucinated_action_is_rejected_before_execution` and
`test_action_without_signoff_is_rejected_by_executor` above).

**Per-engine health, including the Oracle blast-radius split:**

```bash
curl -s "https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/database/health?db=oracle"
curl -s "https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/database/health?db=alloydb"
curl -s "https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/database/health?db=mysql"
```

This is the same endpoint used to verify the Toolbox blast-radius split
live (Section 1 of `CASE_STUDY.md`) — stopping the Oracle-side Toolbox
container and confirming `?db=alloydb` and `?db=mysql` stayed healthy while
only `?db=oracle` reported unreachable.

## 3. Visual verification — the dashboard itself

No credentials or setup required — open the live URL directly and:

- Switch between Oracle / AlloyDB / MySQL and watch the live per-tick Agent
  Execution Loop update.
- Trigger a staged failure from the dashboard's demo controls and watch a
  Tier 1 or Tier 2 action auto-apply.
- Trigger a Tier 3 scenario and watch the Slack approval card get posted
  and the 900-second countdown run in real time.

## What this file is not

This is not a claim that NEXORA is bug-free, or that 60 passing tests prove
correctness of every code path — it doesn't. It's a record of exactly what
was run, when, and what came back, so any of the numbers elsewhere in this
repo's documentation can be checked rather than trusted.

---
*Last verified: 2026-09-07 · Sneha Deepthi Cheenepalli*
