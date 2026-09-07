#!/usr/bin/env python3
"""
Fixes the AlloyDB/MySQL Toolbox split's reachability: the new Cloud Run
instance's INGRESS_TRAFFIC_INTERNAL_ONLY setting rejected calls from the
orchestrator, because the orchestrator's own vpc_access.egress is
PRIVATE_RANGES_ONLY -- which only routes PRIVATE-IP-destined traffic
through the connector, so a call to the Toolbox instance's public *.run.app
hostname went out via the normal internet path instead and got rejected by
Google's edge before ever reaching the container (a Google-edge 404, not a
Toolbox one -- confirmed by the Toolbox server's own logs showing it
started clean with all 20 tools loaded).

Fix: switch the new Toolbox instance to the SAME trust model
cloudrun.tf's own comment already documents for the orchestrator service
itself -- "IAM, not network topology, is what keeps it private" -- instead
of network-internal-only. This needs no change to the orchestrator's shared
egress setting at all (so Slack/Gemini/BigQuery calls, which also go out
that same egress path, are completely unaffected).

Two edits:
  - mcp_toolbox_cloudrun.tf : drop internal-only ingress + allUsers invoker,
                              restrict invoker to the orchestrator's own SA
  - db_tools.py             : get_sync_client_for() attaches a per-request
                              OIDC identity token, reusing the exact
                              get_sync_client()/_fetch_id_token() pattern
                              already proven live against Toolbox 1.9.0
                              elsewhere in this file -- just parameterized
                              by URL instead of hardcoded to toolbox_url()

Safety: same anchor-verified, abort-if-mismatch pattern as every other
patch here. Backs up both files to <name>.bak.pretoolboxauthfix first.
"""
import os

TF_PATH = "gcp_deploy/terraform/mcp_toolbox_cloudrun.tf"
PY_PATH = "gcp_deploy/services/orchestrator/db_tools.py"


def verify_once(content, anchor, fname):
    n = content.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT: expected exactly 1 match for anchor in {fname}, found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


# ---------------------------------------------------------------------
# 1. mcp_toolbox_cloudrun.tf -- two edits
# ---------------------------------------------------------------------
with open(TF_PATH) as f:
    tf_src = f.read()

tf_anchor_ingress = (
    'resource "google_cloud_run_v2_service" "toolbox_alloydb_mysql" {\n'
    '  name                = "toolbox-alloydb-mysql"\n'
    '  location            = var.region\n'
    '  deletion_protection = false\n'
    '  ingress             = "INGRESS_TRAFFIC_INTERNAL_ONLY"\n'
)
verify_once(tf_src, tf_anchor_ingress, f"{TF_PATH} (ingress line)")
tf_replacement_ingress = (
    'resource "google_cloud_run_v2_service" "toolbox_alloydb_mysql" {\n'
    '  name                = "toolbox-alloydb-mysql"\n'
    '  location            = var.region\n'
    '  deletion_protection = false\n'
    '  # Reachability fix (was INGRESS_TRAFFIC_INTERNAL_ONLY): the orchestrator\'s\n'
    '  # own vpc_access.egress is PRIVATE_RANGES_ONLY, which does not route calls\n'
    '  # to this service\'s public *.run.app hostname through the connector -- so\n'
    '  # an internal-only ingress here rejected every orchestrator call at\n'
    '  # Google\'s edge before it ever reached the container. Default ingress\n'
    '  # (this field omitted) plus IAM-restricted invoker below matches the\n'
    '  # exact trust model cloudrun.tf\'s own comment already documents for the\n'
    '  # orchestrator service itself -- "IAM, not network topology, is what\n'
    '  # keeps it private" -- rather than widening the orchestrator\'s shared\n'
    '  # egress setting (which also carries Slack/Gemini/BigQuery traffic).\n'
)
tf_new = tf_src.replace(tf_anchor_ingress, tf_replacement_ingress, 1)

tf_anchor_invoker = (
    '# Ingress is already internal-only (see trust-model note above) -- allUsers\n'
    '# invoker here is the deliberate, documented choice, not an oversight.\n'
    'resource "google_cloud_run_v2_service_iam_member" "toolbox_alloydb_mysql_internal_invoker" {\n'
    '  name     = google_cloud_run_v2_service.toolbox_alloydb_mysql.name\n'
    '  location = var.region\n'
    '  role     = "roles/run.invoker"\n'
    '  member   = "allUsers"\n'
    '}\n'
)
verify_once(tf_new, tf_anchor_invoker, f"{TF_PATH} (invoker member)")
tf_replacement_invoker = (
    '# IAM-restricted invoker (see reachability-fix note above) -- only the\n'
    '# orchestrator\'s own service account may call this service, same pattern\n'
    '# cloudrun.tf uses for scheduler_sa -> orchestrator.\n'
    'resource "google_cloud_run_v2_service_iam_member" "toolbox_alloydb_mysql_orchestrator_invoker" {\n'
    '  name     = google_cloud_run_v2_service.toolbox_alloydb_mysql.name\n'
    '  location = var.region\n'
    '  role     = "roles/run.invoker"\n'
    '  member   = "serviceAccount:${google_service_account.orchestrator_sa.email}"\n'
    '}\n'
)
tf_new = tf_new.replace(tf_anchor_invoker, tf_replacement_invoker, 1)

# ---------------------------------------------------------------------
# 2. db_tools.py -- one edit: attach a per-request OIDC token, reusing the
#    exact fetch_id_token pattern already proven at get_sync_client()'s
#    auth-enabled branch, parameterized by URL instead of hardcoded to the
#    Oracle VM's toolbox_url().
# ---------------------------------------------------------------------
with open(PY_PATH) as f:
    py_src = f.read()

py_anchor = (
    "    if url not in _toolbox_client_cache:\n"
    "        _toolbox_client_cache[url] = ToolboxSyncClient(url)\n"
    "    return _toolbox_client_cache[url]\n"
)
verify_once(py_src, py_anchor, f"{PY_PATH} (get_sync_client_for client construction)")
py_replacement = (
    "    if url not in _toolbox_client_cache:\n"
    "        # Reachability fix: this instance is IAM-gated now (see\n"
    "        # mcp_toolbox_cloudrun.tf), so every call needs a fresh OIDC\n"
    "        # identity token attached -- reusing the exact\n"
    "        # fetch_id_token(Request(), <audience>) call get_sync_client()'s own\n"
    "        # auth-enabled branch already uses (proven live against Toolbox\n"
    "        # 1.9.0), just parameterized by URL instead of hardcoded to\n"
    "        # toolbox_url() -- the audience MUST be this specific service's URL,\n"
    "        # not the Oracle VM's, or Cloud Run's IAM check rejects the token.\n"
    "        # A lambda (not a precomputed string) is required here, not just\n"
    "        # convenient: ID tokens expire (~1hr), and this client is cached\n"
    "        # and reused across many ticks, so the header has to be\n"
    "        # recomputed on every single call rather than fixed at\n"
    "        # construction time.\n"
    "        headers = {\"Authorization\": lambda u=url: f\"Bearer {_fetch_id_token_for(u)}\"}\n"
    "        _toolbox_client_cache[url] = ToolboxSyncClient(url, client_headers=headers)\n"
    "    return _toolbox_client_cache[url]\n"
)
py_new = py_src.replace(py_anchor, py_replacement, 1)

py_helper_anchor = (
    "def _fetch_id_token() -> str:\n"
    "    from google.auth.transport.requests import Request\n"
    "    from google.oauth2.id_token import fetch_id_token\n"
    "\n"
    "    return fetch_id_token(Request(), toolbox_url())\n"
)
verify_once(py_new, py_helper_anchor, f"{PY_PATH} (_fetch_id_token helper)")
py_helper_replacement = py_helper_anchor + (
    "\n"
    "\n"
    "def _fetch_id_token_for(url: str) -> str:\n"
    "    \"\"\"Same as _fetch_id_token() above, but for an arbitrary audience URL --\n"
    "    used by get_sync_client_for()'s split-Toolbox path, where the audience is\n"
    "    the AlloyDB/MySQL Cloud Run instance, not the Oracle VM's toolbox_url().\"\"\"\n"
    "    from google.auth.transport.requests import Request\n"
    "    from google.oauth2.id_token import fetch_id_token\n"
    "\n"
    "    return fetch_id_token(Request(), url)\n"
)
py_new = py_new.replace(py_helper_anchor, py_helper_replacement, 1)

# ---------------------------------------------------------------------
# All anchors verified -- now back up and write both files.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (TF_PATH, tf_src, tf_new),
    (PY_PATH, py_src, py_new),
]:
    backup_path = path + ".bak.pretoolboxauthfix"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nBoth files patched successfully.")
print("Next: terraform plan/apply for toolbox_alloydb_mysql + the new invoker")
print("resource, delete the old allUsers invoker resource from state, then")
print("rebuild+redeploy the orchestrator image.")
