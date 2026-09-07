#!/usr/bin/env python3
"""
Adds TOOLBOX_URL_ALLOYDB_MYSQL to the orchestrator Cloud Run service's env
block in gcp_deploy/terraform/cloudrun.tf, right next to the existing
TOOLBOX_URL/TOOLBOX_REQUIRE_AUTH pair -- so db_tools.get_sync_client_for()
(apply_toolbox_split_code.py) has a real value to read once this is applied.

Run this LAST, after mcp_toolbox_cloudrun.tf's `terraform apply` has
produced the toolbox_alloydb_mysql_url output -- this script prompts for
that URL rather than guessing it, since it's assigned by Cloud Run at
creation time and isn't knowable in advance.

Safety: verifies the anchor exists exactly once before writing anything.
Backs up to cloudrun.tf.bak.pretoolboxenv first. This edits a file this
project has already flagged has known apply-time state drift risk on
adjacent resources (see mcp_toolbox_cloudrun.tf's own comments and
cloudrun.tf's task #36 history) -- run `terraform plan` (not a bare apply)
and read it before applying, same discipline already used for every other
change to this file.
"""
import os
import sys

path = "gcp_deploy/terraform/cloudrun.tf"
with open(path) as f:
    content = f.read()

anchor = (
    '      env {\n'
    '        name  = "TOOLBOX_REQUIRE_AUTH"\n'
    '        value = "false"\n'
    '      }\n'
)
n = content.count(anchor)
if n != 1:
    sys.exit(
        f"ABORT: expected exactly 1 match for anchor in {path}, found {n}.\n"
        f"--- anchor ---\n{anchor}\n--------------\n"
        f"No files have been modified. Paste this error back for a corrected patch."
    )

url = os.environ.get("TOOLBOX_ALLOYDB_MYSQL_URL") or input(
    "Paste the toolbox_alloydb_mysql_url terraform output (or Ctrl-C to abort): "
).strip()
if not url:
    sys.exit("ABORT: no URL provided. No changes written.")

addition = (
    '      env {\n'
    f'        name  = "TOOLBOX_URL_ALLOYDB_MYSQL"\n'
    f'        value = "{url}"\n'
    '      }\n'
)
new_content = content.replace(anchor, anchor + addition, 1)

backup_path = path + ".bak.pretoolboxenv"
with open(backup_path, "w") as f:
    f.write(content)
with open(path, "w") as f:
    f.write(new_content)

print(f"OK: patched {path} (backup at {backup_path})")
print("Next: terraform plan (read it!), then terraform apply -target=... for")
print("just the orchestrator service, then redeploy the orchestrator image so")
print("the running container actually sees the new env var.")
