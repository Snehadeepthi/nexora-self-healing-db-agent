#!/usr/bin/env python3
"""
Fixes a real bug uncovered while verifying the suppression auto-clear flow,
not just a test-methodology workaround: expire_stale_approvals(engine_id)
(guardrail_callbacks.py) only ever expires pending_approvals entries whose
incident_id starts with "{engine_id}-" -- pipeline.py's own real incidents
follow that convention (see its call site,
`self.guardrails.expire_stale_approvals(self.engine.id)`), but all three
/demo/trigger-tier3* routes in main.py build incident_id as
f"demo-tier3[-engine]-{uuid}" -- never "oracle-...", "alloydb-...", or
"mysql-...". A Tier 3 approval created through any of these demo endpoints
therefore never matches the filter and sits in pending_approvals forever,
regardless of TIER3_APPROVAL_TTL_SECONDS -- silently defeating the TTL
safeguard for anyone using the demo trigger to exercise it, not just this
verification run.

Fix: prefix each incident_id with the engine's own id (read off the same
`orch` object each route already builds, via orch.engine.id -- the exact
attribute pipeline.py's self.engine.id already proves exists on this
class) so it lines up with the "{engine_id}-..." convention
expire_stale_approvals expects. Confirmed via grep that no other code
(dashboard.html, BigQuery queries, other routes) depends on the literal
"demo-tier3-" prefix, so this is safe to change.

Three anchor-verified edits (oracle / alloydb / mysql routes). Backs up to
main.py.bak.predemoincidentidfix first.
"""
PATH = "gcp_deploy/services/orchestrator/main.py"

with open(PATH) as f:
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


edits = [
    (
        "oracle",
        '    orch = state()\n'
        '    incident_id = f"demo-tier3-{uuid.uuid4().hex[:8]}"\n',
        '    orch = state()\n'
        '    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"\n',
    ),
    (
        "alloydb",
        '    orch = state("alloydb")\n'
        '    incident_id = f"demo-tier3-alloydb-{uuid.uuid4().hex[:8]}"\n',
        '    orch = state("alloydb")\n'
        '    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"\n',
    ),
    (
        "mysql",
        '    orch = state("mysql")\n'
        '    incident_id = f"demo-tier3-mysql-{uuid.uuid4().hex[:8]}"\n',
        '    orch = state("mysql")\n'
        '    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"\n',
    ),
]

for label, anchor, replacement in edits:
    verify_once(content, anchor, f"{label} demo-tier3 incident_id")
    content = content.replace(anchor, replacement, 1)

backup_path = PATH + ".bak.predemoincidentidfix"
with open(backup_path, "w") as f:
    f.write(original)
with open(PATH, "w") as f:
    f.write(content)

print(f"OK: patched {PATH} (backup at {backup_path})")
print("All 3 demo-tier3* routes now namespace incident_id as '{engine_id}-demo-tier3-...',")
print("matching expire_stale_approvals' filter. Redeploy, then re-trigger")
print("/demo/trigger-tier3 fresh -- the earlier stuck incident_id from before this")
print("fix (demo-tier3-e3956973) is harmless and will be cleared automatically since")
print("pending_approvals is in-memory state that resets on the new container revision.")
