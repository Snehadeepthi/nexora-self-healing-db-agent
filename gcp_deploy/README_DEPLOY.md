# Deploying to GCP — step by step

This turns the local reference implementation into a real, running pipeline on
your GCP project, built on **Google's Agent Development Kit (ADK)** and
**MCP Toolbox for Databases** (Google's open-source agent framework and
open-source MCP database-connectivity server) rather than the hand-rolled
pipeline the original local reference implementation used: a real Oracle
database (spoken to only through MCP Toolbox — the orchestrator itself
never touches Oracle directly), real Vertex AI Gemini calls via ADK's native
tool-calling, a real BigQuery audit trail, real Slack notifications, running
on a schedule. No part of this runs in Claude's sandbox — you run every
command yourself, in **Google Cloud Shell**, which is a free terminal built
into the GCP console that's already logged into your account.

Total hands-on time: about 15–20 minutes. Most of that is waiting for
Terraform, the Oracle VM boot, and Cloud Build, not typing.

There are two ways to run this system: **locally** with `adk_agent/` (a
`docker-compose.yml` running Oracle XE + Toolbox on your machine, no GCP
needed at all — see `adk_agent/README`-style comments in that package's
files) and **in GCP**, which is what the rest of this document covers.
Both run the exact same `agent.py` / `guardrail_callbacks.py` / `pipeline.py`
— only the database/audit/notification backends differ, dependency-injected
at startup (see `gcp_deploy/services/orchestrator/main.py`'s docstring).

## What you're deploying

| Component | Local reference file | Becomes |
|---|---|---|
| Database | `simulators/oracle_simulator.py` | Oracle Database XE on a Compute Engine VM (private, no public IP) |
| Database access | -- | **MCP Toolbox for Databases**, a separate Cloud Run service (`mcp_toolbox.tf`) — the ONLY thing that ever runs SQL against Oracle; tool definitions in `gcp_deploy/tools_db/tools.yaml`, one per `config.ALLOWLIST` entry |
| Sense + Predict + Learn | `sense.py`, `predict.py`, `learn.py` | Unchanged — running inside the orchestrator Cloud Run service, reading telemetry through Toolbox instead of a driver/simulator |
| Reason + Act | `reason.py` + `act.py` | A single **ADK `LlmAgent`** turn (`agent.py`) — Gemini proposes a tool call natively via MCP/function-calling instead of JSON-mode text parsing; every guardrail from the original `act.py`/`reason.py` is now an ADK callback (`guardrail_callbacks.py`) |
| Audit trail | `audit.py` | Real BigQuery table (`gcp_deploy/services/orchestrator/gcp_audit.py`) |
| Runbook store | `runbooks.py` | Real BigQuery table (`gcp_deploy/services/orchestrator/gcp_runbooks.py`) |
| Notifications | `notifications.py` | Real Slack webhook (`gcp_deploy/services/orchestrator/gcp_notifications.py`) |
| Trigger | `demo.py`'s loop | Cloud Scheduler, once a minute |
| CI gate | `test_safety.py` (root) | `gcp_deploy/services/orchestrator/test_safety.py` runs inside Cloud Build before every deploy — 22 tests against the ADK guardrail callbacks, the tests that actually gate what's deployed now (see cloudbuild.yaml's comment) |

Every guardrail file (`config.py`, `allowlist_governor.py`,
`circuit_breaker.py`, `cost_guard.py`, `predict.py`) is copied into the
deployment **completely unchanged** — same as before this rebuild. What
changed is *where the guardrail logic runs*: `act.py`/`reason.py`'s method
calls became `guardrail_callbacks.py`'s ADK callbacks, doing the exact same
checks (allowlist signoff, hallucination firewall, Tier 3 approval gate,
circuit breakers, cost cap) at the same choke points.

**Honest limitation:** `restart_listener` (the Tier 3 action) requires
running `lsnrctl restart` on the database host's OS — not SQL, so MCP
Toolbox can't run it either. It's wired to fail with a clear error rather
than pretend to succeed — see `adk_agent/db_tools.py`'s `restart_listener()`
docstring for how to close that gap (SSH via OS Login, or a small on-VM
control agent) before relying on it for real.

## Cost

The Oracle VM (`e2-medium`) is the only thing that runs 24/7: roughly
**$25–35/month** if you leave it up. Everything else — the two Cloud Run
services (orchestrator + MCP Toolbox), BigQuery, Cloud Scheduler, Gemini
calls — is pay-per-use and should be a few dollars a month at demo volume
(the Toolbox service is pinned to `min_instance_count = 1` for demo
responsiveness — see `mcp_toolbox.tf`'s comment if you'd rather let it scale
to zero and eat an occasional cold start). **Step 6 below tells you how to
tear it all down** when you're done with the pitch, which stops the Oracle
VM cost entirely.

## Prerequisites

- A GCP project with billing enabled (you said you have one).
- Your account needs **Owner** or **Editor** role on that project, or
  equivalently: Compute Admin, Cloud Run Admin, BigQuery Admin, Secret
  Manager Admin, Service Account Admin, Project IAM Admin, Service Usage
  Admin. If you're not sure, Owner is simplest for a demo project.
- (Optional but recommended) A Slack Incoming Webhook URL, so approval
  requests and status reports actually go somewhere real:
  https://api.slack.com/messaging/webhooks — takes 2 minutes, pick any
  channel. You can skip this and add it later.

## Step 1 — Get the project files into Cloud Shell

1. Open https://console.cloud.google.com, make sure the **project selector**
   at the top shows the project you want to deploy into.
2. Click the **Cloud Shell** icon (`>_`) in the top-right toolbar. A terminal
   opens at the bottom of the browser.
3. In Cloud Shell, click the **three-dot menu → Upload** and upload the
   `self_healing_db_agent_gcp_deploy.zip` file you were given.
4. In the Cloud Shell terminal:
   ```bash
   unzip self_healing_db_agent_gcp_deploy.zip -d self_healing_db_agent
   cd self_healing_db_agent
   ls
   ```
   You should see `config.py`, `test_safety.py`, `gcp_deploy/`, `adk_agent/`
   (the ADK + MCP Toolbox rebuild — `gcp_deploy/services/orchestrator` is
   this same package wired to real GCP backends), and the rest of the
   original reference implementation.

## Step 2 — Confirm the CI safety gate still passes here

```bash
pip install -r requirements.txt --quiet
pytest test_safety.py -v
```
You should see the same `17 passed` you saw earlier (this checks the
original, non-ADK reference implementation, kept in place unmodified).

Also check the ADK rebuild's own guardrail tests locally, before spending a
Cloud Build run on it — this is the suite that actually gates the deploy:
```bash
cd gcp_deploy/services/orchestrator
pip install -r requirements.txt pytest --quiet
pytest test_safety.py -v
cd ../../..
```
You should see `22 passed`. If either suite fails, stop here — nothing
downstream should deploy on top of a failing safety gate.

## Step 3 — Configure your deployment

```bash
cp gcp_deploy/terraform/terraform.tfvars.example gcp_deploy/terraform/terraform.tfvars
nano gcp_deploy/terraform/terraform.tfvars
```
Fill in:
- `project_id` — your GCP project ID (not the project *name* — check with
  `gcloud config get-value project` if unsure)
- `oracle_db_password` — pick a real password
- `notify_slack_webhook_url` — your Slack webhook URL, or leave blank

Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

## Step 4 — Enable the two APIs Terraform needs to bootstrap itself

```bash
gcloud services enable serviceusage.googleapis.com cloudresourcemanager.googleapis.com
```
(Everything else — Compute, Cloud Run, BigQuery, Vertex AI, Secret Manager,
Cloud Build, Cloud Scheduler, VPC Access, Artifact Registry — gets enabled
automatically by Terraform in the next step.)

## Step 5 — Deploy

```bash
bash gcp_deploy/deploy.sh
```
This runs `terraform apply` (creates the VPC, the Oracle VM, BigQuery
tables, service accounts, secrets, and the Cloud Run service shell — it will
ask you to confirm before creating anything billable), then runs the CI
safety gate again inside Cloud Build, builds the container image, pushes it,
and deploys it. Takes about 8–10 minutes, mostly the Oracle VM booting and
Cloud Build building the image.

At the end it prints your Cloud Run URL and the exact `curl` commands to
test it — keep that output, or re-run:
```bash
cd gcp_deploy/terraform && terraform output orchestrator_url && cd ../..
```

Prefer to run the Terraform/Cloud Build steps yourself instead of the
wrapper script? Open `gcp_deploy/deploy.sh` — it's a short, readable script;
every command in it is safe to copy-paste one at a time instead.

## Step 6 — Verify it's actually running

Cloud Scheduler is now calling `POST /tick` every minute automatically.
Watch it happen:
```bash
gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=self-healing-orchestrator"
```

To call the service yourself (it's private — IAM-gated, not public — so
grant yourself invoke access first):
```bash
REGION=$(grep '^region' gcp_deploy/terraform/terraform.tfvars | sed -E 's/.*=\s*"(.*)"/\1/')
URL=$(cd gcp_deploy/terraform && terraform output -raw orchestrator_url)

gcloud run services add-iam-policy-binding self-healing-orchestrator \
  --region="$REGION" \
  --member="user:$(gcloud config get-value account)" \
  --role="roles/run.invoker"

curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/health"
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/tick"
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/compliance"
```

**To demo the Tier 3 human-approval flow live** (without waiting for real
Oracle telemetry to organically produce a listener-outage incident):
```bash
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/demo/trigger-tier3"
# -> {"status": "PENDING_APPROVAL", "incident_id": "demo-tier3-xxxxxxxx"}
# Check Slack -- a real approval request should have just landed there.
# Then, as the human approving it:
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/approve/demo-tier3-xxxxxxxx"
```

To generate a genuine Tier 1 incident and watch it auto-heal, connect to the
Oracle VM (`gcloud compute ssh self-healing-oracle-db --zone=<your zone> --tunnel-through-iap`,
password from `terraform output` / what you set in `terraform.tfvars`) and
open enough sessions to push `active_blocked_sessions` past 8 for two ticks
in a row — or just let the Slack webhook receive whatever real database
activity happens naturally in your test instance.

## Step 7 — Tear it down when you're done

This is the step that actually stops the ~$25–35/month Oracle VM cost:
```bash
cd gcp_deploy/terraform
terraform destroy
```
Review what it's about to delete, then confirm. This removes everything
Terraform created — the VM, Cloud Run service, BigQuery dataset (including
your audit trail — export it first with `bq extract` if you want to keep
it), secrets, and networking.

## If something goes wrong

- **`terraform apply` fails on a permissions error** — your account likely
  needs a broader role (see Prerequisites above). The exact error names the
  missing permission.
- **Cloud Build fails on the safety gate step** — good, that's the gate
  doing its job; something regressed. Fix it and re-run
  `bash gcp_deploy/deploy.sh` (Terraform will skip anything already created).
- **Cloud Build fails pushing/deploying with a permissions error** — the
  Terraform in `iam.tf` already grants both the legacy Cloud Build SA and
  the Compute Engine default SA `roles/artifactregistry.writer` and
  `roles/run.admin` (Google changed which one newer projects use by
  default, so this package grants both to avoid guessing which applies to
  your project). If it's still failing, the grant may not have propagated
  yet — wait a minute and re-run
  `gcloud builds submit --config=gcp_deploy/cloudbuild.yaml .` from the
  project root.
- **`could not resolve source` / `storage.objects.get denied` on the
  Compute Engine default service account** — this project defaults Cloud
  Build to running as `PROJECT_NUMBER-compute@developer.gserviceaccount.com`
  rather than the legacy Cloud Build SA, and that account needs read access
  to the bucket Cloud Build stages your source in. `iam.tf` in this package
  already grants it `roles/storage.objectViewer` and `roles/logging.logWriter`
  — if you deployed with an earlier copy of this package before that was
  added, either re-run `terraform apply` in `gcp_deploy/terraform` to pick
  up the new grants, or apply it directly:
  ```bash
  PROJECT_ID=$(gcloud config get-value project)
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
    --role="roles/storage.objectViewer"
  ```
- **`name unknown: Repository "self-healing-agent" not found` on the
  `push-image` step** — the Artifact Registry repository that `terraform
  apply` is supposed to create (`cloudrun.tf`'s
  `google_artifact_registry_repository.repo`) doesn't exist yet in the
  region Cloud Build is pushing to. Two likely causes: (1) an earlier
  `terraform apply` hit a permissions error partway through (e.g. the
  `iam.serviceAccounts.get` issue above) and never got to creating the
  repo — re-run `terraform apply` from `gcp_deploy/terraform`, it's safe to
  re-run and only creates what's missing; or (2) you set a non-default
  `region` in `terraform.tfvars` but `gcloud builds submit --region=...`
  only sets *where Cloud Build executes*, not the `_REGION` substitution
  `cloudbuild.yaml` uses to build the image path — those are two different
  things with confusingly similar names. Check both:
  ```bash
  gcloud artifacts repositories list --project="$(gcloud config get-value project)"
  ```
  If it's empty, re-run `terraform apply`. Either way, always pass the
  substitution explicitly so the pushed image path matches your real
  region (this is now baked into `deploy.sh`, but if you're running the
  `gcloud builds submit` command by hand, add it yourself):
  ```bash
  REGION=$(grep '^region' gcp_deploy/terraform/terraform.tfvars | sed -E 's/.*=\s*"(.*)"/\1/')
  gcloud beta builds submit --config=gcp_deploy/cloudbuild.yaml --substitutions=_REGION="$REGION" .
  ```
- **`iam.serviceAccounts.get` denied even with Owner** — usually IAM
  propagation lag (can take a couple of minutes); retry. If it persists,
  an organization policy or IAM Deny policy above the project may be
  blocking it — that needs to be lifted by whoever administers the Cloud
  org, not something fixable from inside the project alone.
- **`/tick` returns `oracle_unreachable`** — the Oracle XE container can take
  a couple of minutes to finish initializing after the VM first boots.
  Give it 2–3 minutes and try again.
- **`/tick` or `/demo/trigger-tier3` errors with an MCP connection failure
  (timeout, connection refused, or a protocol/handshake error talking to
  Toolbox)** — `adk_agent/db_tools.py` has a flagged, unverified assumption:
  it connects to the Toolbox service's bare base URL, since that's what the
  `toolbox-core` SDK's own `ToolboxSyncClient` does by default. This was
  built and unit-tested against the real `google-adk`/`toolbox-core`
  packages, but never against a live Toolbox server (this sandbox can't
  reach one) — if your deployed Toolbox version exposes its MCP endpoint at
  a different path, edit `TOOLBOX_URL` in `cloudrun.tf`'s orchestrator env
  vars (or the `TOOLBOX_URL` environment variable if running
  `adk_agent/main.py` locally) to include that path. Check the Toolbox
  service's own logs for what path it's actually listening on:
  ```bash
  gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=self-healing-mcp-toolbox" --limit=50 --format="value(textPayload)"
  ```
- **`/tick` or `/demo/trigger-tier3` errors with a 403 calling Toolbox** —
  `mcp_toolbox.tf` only grants `roles/run.invoker` on the Toolbox service to
  `orchestrator_sa`, and `db_tools.py` only attaches an OIDC identity token
  when `TOOLBOX_REQUIRE_AUTH=true` (set on the orchestrator's own env vars
  in `cloudrun.tf`). If you changed either side independently they can drift
  out of sync — re-run `terraform apply` to reconcile both, or check
  `gcloud run services get-iam-policy self-healing-mcp-toolbox --region=<region>`
  shows `orchestrator_sa`'s email with `roles/run.invoker`.
- **Anything else** — `gcloud builds log <BUILD_ID>` and the Cloud Logging
  command in Step 6 are your two best sources of what actually happened.
