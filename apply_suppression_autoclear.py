#!/usr/bin/env python3
"""
Applies the Tier 3 suppression auto-clear fix across 3 files:
  - config.py               : add SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS constant
  - guardrail_callbacks.py  : Guardrails.record_tick_health() -- tracks a
                              per-engine consecutive-healthy-tick streak and
                              auto-clears that engine's suppressions once the
                              streak hits the threshold
  - pipeline.py             : call record_tick_health() once per tick, right
                              after Predict resolves whether this reading is
                              a confirmed anomaly or not

Context: the flooding fix (apply_tier3_flooding_fix.py) made suppression
CLEARING an explicit operator action on purpose (see
guardrail_callbacks.py's expire_stale_approvals docstring: "a fix that
silently stopped applying itself can't go unnoticed"). This patch adds an
opt-in-by-default AUTOMATED path alongside that manual one: if THIS
engine's own Sense/Predict stage reports a clean reading (no confirmed
anomaly) for config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS consecutive ticks,
every suppression still armed for that engine is cleared automatically and
logged as `action_suppression_auto_cleared` -- a distinct event type from
the operator-driven `suppression_cleared`, so the audit trail always shows
which path did the clearing. A single unhealthy tick (confirmed anomaly)
resets the streak to zero. A tick that never reaches Predict at all (breaker
open, Sense poll failure) is simply not counted either way -- it neither
advances nor resets the streak, since a failed poll is not a confirmation of
anything. This means an outage that trips the breaker can't accidentally
un-suppress itself just by going quiet; only a genuinely completed, clean
Predict read counts.

Safety: verifies every anchor text exists EXACTLY ONCE in its target file
BEFORE writing anything. If any single check fails, the whole script aborts
with no files modified. Backs up every file to <name>.bak.preautoclear first.

Anchors below are taken from this project's own already-applied and
smoke-test-verified patches (apply_tier3_ttl_fix.py, apply_tier3_flooding_fix.py)
wherever this patch touches the same code those did, so they should match
byte-for-byte. If any anchor still aborts, paste the error back for a
corrected patch -- same recovery loop as every other fix in this project.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"


def load(fname):
    path = os.path.join(ROOT, fname)
    with open(path, "r") as f:
        return path, f.read()


def verify_once(content, anchor, fname):
    n = content.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT: expected exactly 1 match for anchor in {fname}, found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


# ---------------------------------------------------------------------
# 1. config.py -- new constant, right after the TTL constant it pairs with
# ---------------------------------------------------------------------
config_path, config_src = load("config.py")
config_anchor = "TIER3_APPROVAL_TTL_SECONDS = 900\n"
verify_once(config_src, config_anchor, "config.py")
config_addition = (
    "\n"
    "# Tier 3 suppression auto-clear: normally clear_suppression() is an\n"
    "# explicit operator action (see guardrail_callbacks.py's\n"
    "# expire_stale_approvals docstring for why that's deliberate) -- but\n"
    "# requiring a human to notice and reset it after every transient trip\n"
    "# adds toil for the common case where the underlying condition really\n"
    "# did resolve on its own. If an engine's own Sense/Predict stage reports\n"
    "# a clean reading (no confirmed anomaly) for this many CONSECUTIVE ticks,\n"
    "# every suppression still armed for that engine is auto-cleared (see\n"
    "# Guardrails.record_tick_health()) and logged as\n"
    "# action_suppression_auto_cleared -- a distinct event from the manual\n"
    "# suppression_cleared, so the audit trail always shows which path acted.\n"
    "# 3 ticks x the 60s Cloud Scheduler cadence = ~3 minutes of sustained\n"
    "# health before auto-clearing -- long enough that a single lucky quiet\n"
    "# reading during a still-ongoing storm can't un-suppress it prematurely.\n"
    "SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS = 3\n"
)
config_new = config_src.replace(config_anchor, config_anchor + config_addition, 1)

# ---------------------------------------------------------------------
# 2. guardrail_callbacks.py -- two edits
# ---------------------------------------------------------------------
gc_path, gc_src = load("guardrail_callbacks.py")

# 2a. __init__: add the healthy-streak tracker alongside self.suppressed.
gc_anchor_init = (
    '        # Set by expire_stale_approvals() the moment a Tier 3 proposal times out;\n'
    '        # cleared only by clear_suppression() (an operator action, see main.py\'s\n'
    '        # POST /suppressions/clear/<action_key>) -- see before_tool()\'s Tier 3\n'
    '        # branch and expire_stale_approvals()\'s docstring for the full mechanism.\n'
    '        self.suppressed = {}\n'
)
verify_once(gc_src, gc_anchor_init, "guardrail_callbacks.py (__init__ suppressed dict)")
gc_replacement_init = (
    '        # Set by expire_stale_approvals() the moment a Tier 3 proposal times out;\n'
    '        # cleared only by clear_suppression() (an operator action, see main.py\'s\n'
    '        # POST /suppressions/clear/<action_key>) -- see before_tool()\'s Tier 3\n'
    '        # branch and expire_stale_approvals()\'s docstring for the full mechanism.\n'
    '        self.suppressed = {}\n'
    '        # engine_id -> consecutive confirmed-healthy tick count, used only by\n'
    '        # record_tick_health()\'s auto-clear below -- separate from suppressed\n'
    '        # itself so a streak can keep building even on an engine with nothing\n'
    '        # currently suppressed (harmless: the auto-clear loop just finds\n'
    '        # nothing to clear that tick).\n'
    '        self._consecutive_healthy_ticks = {}\n'
)

# 2b. append record_tick_health() right after list_suppressed(), the last
#     method the flooding fix left at the end of the class.
gc_anchor_tail = (
    '    def list_suppressed(self, engine_id: str) -> list:\n'
    '        """Backs main.py\'s GET /suppressions -- same read-only-mirror\n'
    '        pattern as pending_approvals/GET /approvals/pending."""\n'
    '        return [\n'
    '            {\n'
    '                "action_key": action_key, "since": v["since"],\n'
    '                "since_incident_id": v["since_incident_id"],\n'
    '                "repeat_count": v.get("repeat_count", 0),\n'
    '                "last_incident_id": v.get("last_incident_id"),\n'
    '            }\n'
    '            for (eng, action_key), v in self.suppressed.items() if eng == engine_id\n'
    '        ]\n'
)
verify_once(gc_src, gc_anchor_tail, "guardrail_callbacks.py (list_suppressed tail)")
gc_addition_tail = '''
    def record_tick_health(self, engine_id: str, anomaly_detected: bool) -> list:
        """Tier 3 suppression auto-recovery. Called once per engine per tick
        from pipeline.py's run_cycle(), right after Predict resolves whether
        this reading is a confirmed anomaly -- NOT called at all on a tick
        that never reaches Predict (breaker open, Sense poll failure), so an
        outage can't quietly un-suppress itself just by going quiet; only a
        genuinely completed, clean Predict read counts.

        A single anomaly_detected=True resets the streak to zero. Once
        config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS consecutive clean reads
        land, every suppression still armed for this engine is cleared and
        logged as action_suppression_auto_cleared -- distinct from the
        operator-driven suppression_cleared event (see clear_suppression),
        so the audit trail always shows which path did the clearing. Returns
        the (engine_id, action_key) pairs actually cleared, same shape
        clear_suppression() returns, or [] on a tick that didn't trigger a
        clear."""
        if anomaly_detected:
            self._consecutive_healthy_ticks[engine_id] = 0
            return []

        streak = self._consecutive_healthy_ticks.get(engine_id, 0) + 1
        self._consecutive_healthy_ticks[engine_id] = streak
        if streak < config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS:
            return []

        self._consecutive_healthy_ticks[engine_id] = 0
        keys = [k for k in self.suppressed if k[0] == engine_id]
        cleared = []
        for key in keys:
            entry = self.suppressed.pop(key)
            self.audit_log.log(
                "action_suppression_auto_cleared",
                incident_id=entry.get("last_incident_id", "unknown"),
                action_key=key[1],
                detail=(
                    f"Auto-cleared after {config.SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS} "
                    f"consecutive healthy ticks on '{engine_id}' with no confirmed "
                    f"anomaly; had been suppressed since incident "
                    f"{entry.get('since_incident_id', 'unknown')} after "
                    f"{entry.get('repeat_count', 0)} repeat proposal(s)."
                ),
            )
            cleared.append((engine_id, key[1]))
        return cleared
'''
gc_replacement_tail = gc_anchor_tail + gc_addition_tail

gc_new = gc_src.replace(gc_anchor_init, gc_replacement_init, 1)
gc_new = gc_new.replace(gc_anchor_tail, gc_replacement_tail, 1)

# ---------------------------------------------------------------------
# 3. pipeline.py -- call record_tick_health() right where Predict resolves
# ---------------------------------------------------------------------
pipe_path, pipe_src = load("pipeline.py")
pipe_anchor = (
    "            event = self.detector.score(reading)\n"
    "            if event is None:\n"
    '                return {"stage": "predict", "outcome": "no_confirmed_anomaly", "reading": reading}\n'
)
verify_once(pipe_src, pipe_anchor, "pipeline.py (Predict resolution)")
pipe_replacement = (
    "            event = self.detector.score(reading)\n"
    "            self.guardrails.record_tick_health(self.engine.id, anomaly_detected=event is not None)\n"
    "            if event is None:\n"
    '                return {"stage": "predict", "outcome": "no_confirmed_anomaly", "reading": reading}\n'
)
pipe_new = pipe_src.replace(pipe_anchor, pipe_replacement, 1)

# ---------------------------------------------------------------------
# All anchors verified -- now back up and write every file.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (config_path, config_src, config_new),
    (gc_path, gc_src, gc_new),
    (pipe_path, pipe_src, pipe_new),
]:
    backup_path = path + ".bak.preautoclear"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nAll 3 files patched successfully.")
print("Next: run the unit tests (test_suppression_autoclear.py, drop it into")
print(f"{ROOT}/ and run: pytest test_suppression_autoclear.py -v), then redeploy")
print("the same way every other fix in this project has been deployed")
print("(gcloud builds submit --config=gcp_deploy/cloudbuild.yaml ...).")
