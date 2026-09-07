# Pre-Touchpoint-3 hardening -- 3 patches, deployed & verified live

**Status: all 3 items deployed and independently verified against the real
running system on 2026-09-02.** This replaces the original pre-deploy
version of this runbook -- kept below (lightly edited) as the actual
sequence that shipped, including the two real bugs the verification pass
uncovered and fixed along the way. Every fix here went through you running
it in Cloud Shell against the real repo; nothing was assumed working until
proven against live output. *(Note: the `apply_*.py` scripts referenced
below now live under `scripts/migrations/` — moved after this record was
written to keep the repo root uncluttered; paths here reflect the current
location.)*

## 1. Tier 3 suppression auto-clear -- VERIFIED LIVE

```
python3 scripts/migrations/apply_suppression_autoclear.py
cp test_suppression_autoclear.py gcp_deploy/services/orchestrator/
cd gcp_deploy/services/orchestrator && pytest test_suppression_autoclear.py -v && cd -
```
Redeployed via the standard `gcloud builds submit --config=gcp_deploy/cloudbuild.yaml ...`.
7/7 unit tests passed on first run.

**Live verification (2026-09-02, real BigQuery audit trail):**
- `13:27:46` -- `action_pending_approval` (`oracle-demo-tier3-...`, `increase_pga_target`)
- `13:29:06` -- `approval_timeout` (TTL expired, caught by the next tick; this
  also silently arms the suppression flag)
- `13:30:34` -- **`action_suppression_auto_cleared`** -- cleared itself after
  3 consecutive healthy ticks, zero operator action

Full propose -> timeout -> suppress -> auto-clear lifecycle confirmed end to
end from the durable audit trail, not just the API's point-in-time view (the
whole cycle finished faster than expected, so `/suppressions` checked after
the fact correctly showed empty -- already cleared, not broken; the BigQuery
trail is what actually proves it happened).

**Bug found and fixed during verification, not present in the original
patch:** `/demo/trigger-tier3` (and the `-alloydb`/`-mysql` variants) built
`incident_id` as `demo-tier3-{uuid}` -- never `{engine_id}-...`. But
`expire_stale_approvals(engine_id)` only ever expires entries whose
`incident_id` starts with `"{engine_id}-"` (see `pipeline.py`'s
`self.guardrails.expire_stale_approvals(self.engine.id)`). A Tier 3 approval
created through any demo trigger therefore sat in `pending_approvals`
forever, regardless of `TIER3_APPROVAL_TTL_SECONDS` -- silently defeating the
TTL safeguard for anyone exercising it via the demo endpoints, not just this
verification run. Fixed in `main.py` across all 3 demo-tier3 routes:
`incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"`.

**Verification method, for repeating this later:** the real TTL is 900s (15
min), which makes a live run slow. For a fast repeat: temporarily set
`TIER3_APPROVAL_TTL_SECONDS = 30` in `config.py`, redeploy, trigger via
`POST /demo/trigger-tier3`, wait ~90s, then read the audit trail directly --
`/suppressions` and `/incidents/history` are both unreliable narrow windows
for this (the former only shows current state, the latter's incident-id
grouping doesn't recognize the demo-tier3 shape and silently omits it
entirely). Query BigQuery instead:
```
bq query --use_legacy_sql=false --nouse_cache \
  "SELECT ts, event_type, incident_id FROM \`<project>.db_ops.audit_log\` \
   WHERE incident_id LIKE '%demo-tier3%' ORDER BY ts DESC LIMIT 20"
```
Always pass `--nouse_cache` -- a cached empty result before the streaming
buffer settles reads as false-negative "nothing happened." Restore
`TIER3_APPROVAL_TTL_SECONDS` to 900 and redeploy again immediately after any
test run -- do not leave the short TTL live.

## 2. MCP Toolbox blast-radius split -- VERIFIED LIVE

```
python3 scripts/migrations/apply_toolbox_split_code.py          # code: db_tools.py, guardrail_callbacks.py, pipeline.py, main.py
python3 split_toolbox_config.py               # extracts alloydb-db/mysql-db tools into tools_cloudrun.yaml
```
Placeholders resolved via `sed` against real Terraform-output IPs (AlloyDB
`10.120.2.2`, MySQL `10.120.0.3`), pushed to Secret Manager, then
`mcp_toolbox_cloudrun.tf` applied to stand up the new `toolbox-alloydb-mysql`
Cloud Run service, and `scripts/migrations/apply_cloudrun_toolbox_env.py` wired
`TOOLBOX_URL_ALLOYDB_MYSQL` into the orchestrator.

**Bug found and fixed during first live health check, not present in the
original patch:** the new Toolbox instance was deployed with
`ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"` and an `allUsers` invoker. That
combination silently doesn't work here: the orchestrator's own
`vpc_access.egress = "PRIVATE_RANGES_ONLY"` doesn't route calls to the
Toolbox's public `*.run.app` hostname through the VPC connector, so those
calls went out the normal internet path and were rejected by
`INGRESS_TRAFFIC_INTERNAL_ONLY` at Google's edge before ever reaching the
container -- a Google-edge 404, not a Toolbox one (confirmed via Cloud Run
logs showing the Toolbox server itself started clean with all 20 tools
loaded). Ruled out a Toolbox version mismatch first (VM was 1.9.0, Cloud Run
was `:latest`/1.10.0 -- pinned both to 1.9.0, 404s persisted identically,
so that wasn't it).

Fix: switched the Toolbox service to the same "IAM, not network topology"
trust model the orchestrator already uses for itself -- default (public)
ingress plus an invoker IAM binding scoped to just
`serviceAccount:${orchestrator_sa.email}`, with `db_tools.py`'s
`get_sync_client_for()` attaching a per-request OIDC identity token
(`client_headers` with a lambda, reusing the exact `_fetch_id_token()`
pattern already proven live elsewhere in the same file). A second follow-up
fix was needed on top of that: Terraform's `ingress` field on
`google_cloud_run_v2_service` is Optional+Computed, so simply *removing* the
line from config did NOT reset it to the default (`terraform plan` showed
`0 to change` -- proof, not assumption) -- it had to be set to an explicit
`ingress = "INGRESS_TRAFFIC_ALL"` for Terraform to actually detect and apply
the change.

**Live verification (2026-09-02):** all 3 engines healthy through the new
path (`/database/health?db=oracle|alloydb|mysql` all `"status": "healthy"`).
Then the real blast-radius proof: SSH'd into the Oracle VM, `sudo docker stop
toolbox` -- `?db=oracle` correctly flipped to `"status": "unreachable"`
(`Cannot connect to host 10.10.0.14:5000`) while `?db=alloydb` and
`?db=mysql` stayed healthy throughout, unaffected. Container restarted
afterward (`sudo docker start toolbox`), Oracle confirmed healthy again.
Blast radius reduced from 3 engines to 1, proven rather than asserted.

(MySQL's own Cloud Scheduler tick job, `tick_mysql`, was already present in
`cloudrun.tf` by the time this was checked -- the gap flagged in the
original version of this doc is already closed, no action needed.)

## 3. Dashboard "Cost Avoided" ROI panel -- VERIFIED LIVE

```
python3 scripts/migrations/apply_dashboard_roi_panel.py
```
First attempt aborted cleanly (anchor mismatch -- the real
`dashboard.html` uses 6-space indentation and `class="board cols-3"`, not
the 4-space/`class="board"` the script guessed from a cached working copy).
Corrected anchor, re-ran successfully, redeployed.

**Live verification (2026-09-02, screenshot-confirmed):** panel renders
directly under the 5 KPI tiles, showing `$1,073` with the full formula
(`22.5m human MTTA - 1.05m Oracle blocking-session kill x $50/min ~ 21.45m
saved`), live-recomputing incident-class/MTTA/cost-per-minute controls, and
the 15-30 min human-MTTA disclosure text intact.

## What's genuinely still open

- Suppression is keyed on the exact `(engine_id, action_key)` pair, by
  design -- a different allowlisted action for the same underlying condition
  still notifies separately, and nothing auto-clears the flag except the
  healthy-tick counter (an operator can still always manually
  `POST /suppressions/clear/<action_key>`).
- The blast-radius reduction is scoped to AlloyDB/MySQL; Oracle's Toolbox is
  still VM-pinned by design (the documented O5LOGON/VPC-connector
  incompatibility), so Oracle itself remains a single point of failure for
  its own engine -- reduced from 3 engines down to 1, not to 0.
- Cost/minute on the ROI panel is an editable placeholder (this project has
  no real figure for it) -- disclosed inline, not asserted as measured.
