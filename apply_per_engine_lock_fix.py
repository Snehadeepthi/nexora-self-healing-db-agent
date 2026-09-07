#!/usr/bin/env python3
"""
Fixes an accidental cross-engine coupling in pipeline.py's
build_orchestrators(): a single threading.Lock() was created ONCE and
handed to every engine's AdkOrchestrator as pipeline_lock, so Oracle,
AlloyDB, and MySQL all serialize run_cycle()/approve_and_resolve() through
the SAME lock -- not just against their own overlapping ticks (which is
what the lock's own docstring says it's for), but against each other too.

Concretely: if Oracle's onset-to-kill takes 63s while holding that shared
lock, AlloyDB's and MySQL's own independently-scheduled Cloud Scheduler
ticks -- which have nothing to do with Oracle -- queue behind it too. Under
a multi-engine incident (e.g. the lock-cascade scenario), this serializes
all three engines' response loops through one bottleneck at exactly the
worst time.

Fix: give each engine its OWN threading.Lock(), created fresh inside the
loop instead of once outside it. This preserves the lock's actual intended
guarantee (an engine never races against its own overlapping tick) while
removing the unintended cross-engine serialization.

Safety: two independent anchors, each verified to occur exactly once
before anything is written. Backs up the file first; aborts cleanly with
no changes if either anchor doesn't match exactly once.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "pipeline.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()


def verify_once(content, anchor, label):
    n = content.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT: expected exactly 1 match for anchor ({label}), found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


anchor1 = "    shared_lock = threading.Lock()\n    orchestrators = {}\n"
verify_once(src, anchor1, "shared_lock declaration + orchestrators = {}")
replacement1 = "    orchestrators = {}\n"

anchor2 = "            pipeline_lock=shared_lock,\n"
verify_once(src, anchor2, "pipeline_lock=shared_lock")
replacement2 = "            pipeline_lock=threading.Lock(),\n"

new_src = src.replace(anchor1, replacement1, 1)
new_src = new_src.replace(anchor2, replacement2, 1)

backup_path = path + ".bak.perenginelock"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("Each engine's AdkOrchestrator now gets its own threading.Lock() instead of a shared one.")
