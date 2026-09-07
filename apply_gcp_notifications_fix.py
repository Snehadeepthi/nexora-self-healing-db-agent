#!/usr/bin/env python3
"""
Adds send_approval_timeout() to gcp_notifications.py's SlackNotifier class --
the PRODUCTION notifier used by the real GCP deployment (as opposed to the
local/dev notifications.py's EmailNotifier, already patched earlier).

guardrail_callbacks.py's expire_stale_approvals() calls
self.notifier.send_approval_timeout(...) unconditionally -- in production
that notifier is this SlackNotifier, so without this patch the first
Tier 3 approval that actually expires would crash with AttributeError.

IMPORTANT: this file's _send() takes only (self, subject, body) -- no `to`
param -- and returns SentNotification, not SentEmail. This patch matches
THIS file's actual shape; it is deliberately NOT a copy of the
notifications.py patch.

Safety: finds the unique comment line marking the start of
send_status_report(), verifies it occurs exactly once, and inserts the new
method immediately before it (so method order stays
send_approval_request -> send_approval_timeout -> send_status_report).
Indentation is detected from the file itself rather than hardcoded, so this
is robust even if the file's actual whitespace differs slightly from what
was pasted into chat. Backs up the file before writing; aborts cleanly with
no changes if the marker isn't found exactly once.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "gcp_notifications.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()

marker_text = "post-resolution status report"
count = src.count(marker_text)
if count != 1:
    raise SystemExit(
        f"ABORT: expected exactly 1 match for marker text in {FNAME}, found {count}.\n"
        f"No files have been modified. Paste this error back for a corrected patch."
    )

marker_pos = src.index(marker_text)
line_start = src.rfind("\n", 0, marker_pos) + 1
line_end = src.index("\n", marker_pos) + 1
comment_line = src[line_start:line_end]
indent = comment_line[: len(comment_line) - len(comment_line.lstrip())]

method_lines = [
    "",
    f"{indent}# ---- Tier 3 approval TTL expiry notification ----",
    f"{indent}def send_approval_timeout(self, action_payload: dict, detail: str) -> SentNotification:",
    f'{indent}    subject = f"[SRE Agent] Tier 3 approval EXPIRED: {{action_payload.get(\'action_key\')}}"',
    f"{indent}    body = (",
    f'{indent}        f"A Tier 3 action was proposed and awaiting approval, but no one approved it "',
    f'{indent}        f"in time -- it has been automatically aborted WITHOUT executing.\\n\\n"',
    f'{indent}        f"Incident:     {{action_payload.get(\'incident_id\')}}\\n"',
    f'{indent}        f"Action:       {{action_payload.get(\'action_key\')}}\\n"',
    f'{indent}        f"Parameters:   {{action_payload.get(\'params\')}}\\n"',
    f'{indent}        f"Detail:       {{detail}}\\n\\n"',
    f'{indent}        f"The underlying condition that triggered this proposal may still be "',
    f'{indent}        f"unresolved -- check the dashboard for this incident."',
    f"{indent}    )",
    f"{indent}    return self._send(subject, body)",
    "",
]
method_block = "\n".join(method_lines) + "\n"

new_src = src[:line_start] + method_block + src[line_start:]

backup_path = path + ".bak.pretier3ttl"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("send_approval_timeout() added to SlackNotifier, between send_approval_request() and send_status_report().")
