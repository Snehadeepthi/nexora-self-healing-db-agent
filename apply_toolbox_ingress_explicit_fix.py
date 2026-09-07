#!/usr/bin/env python3
"""
Follow-up correction to apply_toolbox_split_auth_fix.py: that script REMOVED
the `ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"` line from
mcp_toolbox_cloudrun.tf, intending "field omitted -> defaults to public
ingress". But `terraform plan` on the real file (pasted back) showed
`0 to change` for the service -- proof that's wrong for this provider field.

google_cloud_run_v2_service.ingress is Optional+Computed: when the argument
is absent from config, Terraform does NOT reset it to the provider default --
it just has "no opinion" and keeps whatever value is already in state. Since
state already has INGRESS_TRAFFIC_INTERNAL_ONLY (from the original apply),
omitting the line would have left it exactly as broken as before, silently,
even after a "successful" apply. The IAM-restricted-invoker half of that fix
is correct and unaffected -- only the ingress field needs a real value.

Fix: add back an EXPLICIT `ingress = "INGRESS_TRAFFIC_ALL"` line (the field's
actual default for a Cloud Run v2 service; there's no "unset" in a targeted
plan) right after the reachability-fix comment block that script already
inserted -- so Terraform sees a real value change, not an absent argument.

Anchor is the exact comment block apply_toolbox_split_auth_fix.py wrote,
confirmed present in your real file since the plan you pasted only showed
the invoker create/destroy (meaning the earlier edit landed) with no anchor
error. Backs up to mcp_toolbox_cloudrun.tf.bak.preingressexplicit first.
"""
TF_PATH = "gcp_deploy/terraform/mcp_toolbox_cloudrun.tf"

with open(TF_PATH) as f:
    content = f.read()
original = content


def verify_once(c, anchor, label):
    n = c.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT [{label}]: expected exactly 1 match, found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


anchor = (
    '  # keeps it private" -- rather than widening the orchestrator\'s shared\n'
    '  # egress setting (which also carries Slack/Gemini/BigQuery traffic).\n'
)
verify_once(content, anchor, "reachability-fix comment block tail")

replacement = anchor + '  ingress             = "INGRESS_TRAFFIC_ALL"\n'
content = content.replace(anchor, replacement, 1)

backup_path = TF_PATH + ".bak.preingressexplicit"
with open(backup_path, "w") as f:
    f.write(original)
with open(TF_PATH, "w") as f:
    f.write(content)

print(f"OK: patched {TF_PATH} (backup at {backup_path})")
print("Now re-run terraform plan (same -target flags as before) -- it should")
print("show 1 to change for google_cloud_run_v2_service.toolbox_alloydb_mysql")
print("this time (ingress: INGRESS_TRAFFIC_INTERNAL_ONLY -> INGRESS_TRAFFIC_ALL),")
print("plus the same 1 to add / 1 to destroy for the invoker as before.")
