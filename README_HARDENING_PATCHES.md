# Pre-Touchpoint-3 hardening -- 3 patches, deploy runbook

All three are written and locally logic-verified (unit tests, a splitter
dry-run, and a headless-browser render of the dashboard panel), but none
have been applied to the real repo or deployed -- I don't have gcloud,
terraform, or a copy of your actual current source tree in this sandbox, so
every fix in this project (including the two already confirmed live) has
gone through you running it in Cloud Shell. These follow the exact same
anchor-verified, abort-safely-if-anything-doesn't-match pattern as
`apply_tier3_ttl_fix.py` and `apply_tier3_flooding_fix.py` -- if a script
aborts, paste the error back for a corrected version rather than editing it
yourself.

Run everything from your project root (`cd self_healing_db_agent`), same as
`gcp_deploy/deploy.sh`.

## 1. Tier 3 suppression auto-clear (lowest risk, do this first)

```
python3 apply_suppression_autoclear.py
cp test_suppression_autoclear.py gcp_deploy/services/orchestrator/
cd gcp_deploy/services/orchestrator && pytest test_suppression_autoclear.py -v && cd -
```

Then redeploy the orchestrator the same way every other fix here has been:
`gcloud builds submit --config=gcp_deploy/cloudbuild.yaml --region=$REGION --substitutions=_REGION=$REGION .`

**Verify live**: force a suppression via `/demo/trigger-tier3` -> let it time
out (or lower `TIER3_APPROVAL_TTL_SECONDS` temporarily like the demo script
already does for the connection-storm threshold) -> confirm `GET
/suppressions` shows it -> let 3 clean ticks pass -> confirm it clears
itself and `action_suppression_auto_cleared` shows up in `/compliance`'s
underlying BigQuery audit log.

## 2. MCP Toolbox blast-radius split

Five steps, in order -- each one only makes sense after the last:

```
python3 apply_toolbox_split_code.py          # code: db_tools.py, guardrail_callbacks.py, pipeline.py, main.py
python3 split_toolbox_config.py               # extracts alloydb-db/mysql-db tools into tools_cloudrun.yaml
```

**Step 3 -- resolve `${VAR}` placeholders and push to Secret Manager.**
Toolbox's own env-var substitution was already found unreliable for at
least one field (`${ORACLE_PASSWORD}`, see `database.tf`), so this project
resolves these with literal `sed` first, same as the VM startup script does
-- don't trust Toolbox to do it:

```
sed \
  -e "s|\${ALLOYDB_HOST}|<real-host>|g" \
  -e "s|\${ALLOYDB_PORT}|<real-port>|g" \
  -e "s|\${ALLOYDB_USER}|<real-user>|g" \
  -e "s|\${ALLOYDB_PASSWORD}|<real-password>|g" \
  -e "s|\${MYSQL_HOST}|<real-host>|g" \
  -e "s|\${MYSQL_PORT}|<real-port>|g" \
  -e "s|\${MYSQL_USER}|<real-user>|g" \
  -e "s|\${MYSQL_PASSWORD}|<real-password>|g" \
  gcp_deploy/tools_db/tools_cloudrun.yaml > gcp_deploy/tools_db/tools_cloudrun.resolved.yaml
```
(Adjust the placeholder names to whatever your actual `alloydb-db`/`mysql-db`
source blocks use -- `split_toolbox_config.py`'s printed tool/source list
tells you exactly what got extracted.)

**Step 4 -- Terraform.**
```
cp mcp_toolbox_cloudrun.tf gcp_deploy/terraform/
cd gcp_deploy/terraform
terraform init -input=false
terraform plan -target=google_secret_manager_secret.toolbox_cloudrun_config \
                -target=google_cloud_run_v2_service.toolbox_alloydb_mysql \
                -target=google_cloud_run_v2_service_iam_member.toolbox_alloydb_mysql_internal_invoker
```
**Read the plan before applying.** `cloudrun.tf`'s own history in this
project (task #36) flags known Terraform state drift on `database.tf`
relative to the live Oracle VM -- `mcp_toolbox_cloudrun.tf` was written to
avoid referencing the VM or the orchestrator service at all so a targeted
apply here can't drag either into a plan, but confirm the plan only shows
the 3 new resources above before typing yes.
```
terraform apply -target=google_secret_manager_secret.toolbox_cloudrun_config \
                 -target=google_cloud_run_v2_service.toolbox_alloydb_mysql \
                 -target=google_cloud_run_v2_service_iam_member.toolbox_alloydb_mysql_internal_invoker
gcloud secrets versions add toolbox-alloydb-mysql-config \
  --data-file=../tools_db/tools_cloudrun.resolved.yaml
terraform output toolbox_alloydb_mysql_url
```
The new Cloud Run revision picks up the secret on its next cold start --
`gcloud run services update toolbox-alloydb-mysql --region=$REGION` (no
other flags) forces one now instead of waiting for one to happen naturally.

**Step 5 -- wire the URL into the orchestrator, then redeploy it.**
```
cd ../..
TOOLBOX_ALLOYDB_MYSQL_URL="$(cd gcp_deploy/terraform && terraform output -raw toolbox_alloydb_mysql_url)" \
  python3 apply_cloudrun_toolbox_env.py
cd gcp_deploy/terraform && terraform plan -target=google_cloud_run_v2_service.orchestrator && terraform apply -target=google_cloud_run_v2_service.orchestrator
cd ../.. && gcloud builds submit --config=gcp_deploy/cloudbuild.yaml --region=$REGION --substitutions=_REGION=$REGION .
```

**Verify live**: `GET /database/health?db=alloydb` and `?db=mysql` should
both succeed with the orchestrator's own Cloud Run logs showing calls going
to the new `toolbox-alloydb-mysql` URL, not the VM. The real test: stop the
VM's `toolbox` container (`docker stop toolbox` over SSH, or just reboot
the VM) and confirm `/database/health?db=alloydb`/`?db=mysql` still report
healthy while `?db=oracle` correctly reports unreachable -- that's the
actual blast-radius claim, proven rather than asserted.

`apply_cloudrun_toolbox_env.py`'s anchor is my best guess at
`cloudrun.tf`'s current orchestrator env block -- I don't have a confirmed-
current copy of that file (only fragments), unlike the other patches here,
so this is the one most likely to abort on the first try. If it does, paste
the surrounding `env { ... }` blocks from your real `cloudrun.tf` back and
I'll fix the anchor.

## 3. Dashboard "Cost Avoided" ROI panel

```
python3 apply_dashboard_roi_panel.py
```
Open the dashboard afterward and confirm the panel renders under the KPI
tiles with live-recomputing numbers -- I test-rendered this against the
closest working copy of `dashboard.html` I had (screenshot-verified in both
light and dark mode), but not your exact live file, so it's the second most
likely of these five scripts to need an anchor correction.

Redeploy: same `gcloud builds submit ...` as above (the dashboard ships
inside the orchestrator's own container image, per `main.py`'s `/` route).

No code/infra change needed beyond this -- the 30-second Slack
approval-flow screen recording the review also asked for is a capture step
for whoever's presenting, not something a patch script can produce.

## What's genuinely still open after all 3 land

- The `record_tick_health` auto-clear and the Toolbox split are both new
  and neither can be marked "verified live" in the docs until you've run
  the verification steps above and it's confirmed working against the real
  deployment -- same discipline the two already-shipped fixes went through
  before the architecture blueprint/overview deck called them done.
- `apply_cloudrun_toolbox_env.py` and (to a lesser extent)
  `apply_dashboard_roi_panel.py` are working from my best reconstruction of
  your current files, not a confirmed-current copy -- expect a possible
  abort-and-retry round on either.
- The MySQL engine doesn't have a Cloud Scheduler tick job in Terraform yet
  (only `tick` for Oracle and `tick_alloydb` exist) -- outside what was
  asked here, but worth flagging since the Toolbox split assumes MySQL is
  already ticking in production; if it isn't yet, add a `tick_mysql`
  resource mirroring `tick_alloydb` before relying on MySQL's auto-clear
  streak ever advancing.
