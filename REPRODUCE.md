# Reproduce

A judge or reviewer should be able to see NEXORA actually work without
taking anything in the README on faith. This document has two independent
paths — pick whichever fits the time you have. Neither requires a GCP
project or credentials of your own.

## Path A — 60 seconds, no setup: the live system

The production system is already running. Nothing to install.

```bash
curl -s https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/compliance
```

or open the dashboard directly:

**[self-healing-orchestrator-casezd3hrq-uc.a.run.app](https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/)**

From there you can switch engines, trigger a staged failure, and watch a
Tier 1/2 auto-fix apply or a Tier 3 Slack approval card get posted with its
live 900-second countdown. See `VERIFICATION.md` for the exact commands and
real responses this was checked against.

## Path B — local, zero dependencies: the reference implementation

This runs the same Sense → Predict → Reason+Act → Learn pipeline entirely
against local simulators — no GCP project, no credentials, no network
access required.

```bash
git clone https://github.com/Snehadeepthi/nexora-self-healing-db-agent.git
cd nexora-self-healing-db-agent
python3 -m venv nexora_venv
source nexora_venv/bin/activate
pip install -r requirements.txt

python demo.py              # narrated end-to-end scenarios
pytest test_safety.py -v    # 17 tests — the safety gate, run against local simulators
```

Expected result: `17 passed`. This is the same command and the same result
recorded in `VERIFICATION.md`.

## Path C — the production orchestrator's own test suite

This exercises the guardrail logic as it's actually wired into the deployed
Cloud Run service (Tier 3 approval emails, the cost guard, the circuit
breaker, the cross-engine short-circuit guard, and the suppression
auto-clear state machine) — without deploying anything yourself.

```bash
cd nexora-self-healing-db-agent
source nexora_venv/bin/activate  # from Path B
pip install -r gcp_deploy/services/orchestrator/requirements.txt

pytest gcp_deploy/services/orchestrator/test_safety.py \
       gcp_deploy/services/orchestrator/test_suppression_autoclear.py -v
```

Expected result: `43 passed`. Together with Path B, that's the full 60/60
recorded in `VERIFICATION.md`.

## Deploying your own copy (optional, requires a GCP project)

The full infrastructure is defined as code under `gcp_deploy/terraform/`. A
sanitized example variables file ships alongside the real (gitignored)
`terraform.tfvars`, so the required inputs are visible without any secret
values being present in the repo. This path is not required to evaluate
NEXORA — Paths A through C above are sufficient — but the Terraform is real
and unredacted for anyone who wants to stand up their own instance.

## What "reproduce" means here

Paths A and C hit the actual deployed system and its actual test suite —
nothing staged or mocked for this document. Path B runs the original local
reference implementation, which is real code with no network dependency,
not a simplified demo written to make a good first impression.

---
*Sneha Deepthi Cheenepalli · Patchamomma 2026*
