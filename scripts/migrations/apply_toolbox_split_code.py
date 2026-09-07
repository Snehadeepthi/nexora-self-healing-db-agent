#!/usr/bin/env python3
"""
Applies the MCP Toolbox blast-radius-split code changes across 4 files:
  - db_tools.py             : new get_sync_client_for(engine) -- routes
                               AlloyDB/MySQL to a second, independent Toolbox
                               client/URL; Oracle's existing get_sync_client()
                               path is untouched
  - guardrail_callbacks.py  : _get_db_client() becomes engine-aware, and
                               _snapshot() passes its already-resolved engine
                               through, so a Tier 3/Tier 1/Tier 2 pre-state
                               snapshot for an AlloyDB or MySQL action reads
                               from the SAME Toolbox instance that action
                               will actually run against
  - pipeline.py             : each AdkOrchestrator's own _db_client is now
                               requested for its own engine, not the shared
                               default
  - main.py                 : /database/info, /statspack/summary, and
                               /database/health all resolve `db_id` from the
                               query string already -- they now also use it
                               to pick the right per-engine orchestrator's
                               client, instead of always reading the DEFAULT
                               engine's client while asking it for a
                               different engine's tools

Context: the MCP Toolbox container currently runs ONLY on the Oracle VM (see
mcp_toolbox.tf's comment for why -- Toolbox-over-the-VPC-connector corrupts
Oracle's O5LOGON handshake, so Toolbox has to be 127.0.0.1 from Oracle's
point of view). Because db_tools.get_sync_client() takes no engine argument
today, EVERY engine -- Oracle, AlloyDB, MySQL -- currently shares that one
VM-pinned Toolbox client, even though only Oracle actually needs to be
there. A VM kernel panic, hardware fault, or compute outage blinds AlloyDB
and MySQL telemetry too, not just Oracle's -- despite neither of them
having any real dependency on that VM.

This patch does NOT touch Oracle's path at all: get_sync_client() (zero
args) is untouched, byte-for-byte, so Oracle keeps talking to the VM-pinned
Toolbox exactly as it does today, zero regression risk there. It ADDS a new,
independent function that AlloyDB/MySQL are switched onto, pointed at
TOOLBOX_URL_ALLOYDB_MYSQL -- the second Cloud Run Toolbox service defined in
mcp_toolbox_cloudrun.tf. See split_toolbox_config.py for extracting that
service's tools.yaml, and gcp_deploy/tools_db/README_TOOLBOX_SPLIT.md (this
patch's companion doc) for the full deploy sequence.

Safety: verifies every anchor text exists EXACTLY ONCE in its target file
BEFORE writing anything. If any single check fails, the whole script aborts
with no files modified. Backs up every file to <name>.bak.pretoolboxsplit
first. If any anchor aborts, paste the error back for a corrected patch --
same recovery loop as every other fix in this project.
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
# 1. db_tools.py -- purely additive: a new function inserted immediately
#    before the existing get_sync_client() definition. Nothing existing is
#    replaced, so this has no way to disturb Oracle's current behavior.
# ---------------------------------------------------------------------
db_path, db_src = load("db_tools.py")
db_anchor = "def get_sync_client() -> ToolboxSyncClient:"
verify_once(db_src, db_anchor, "db_tools.py (get_sync_client definition)")
db_addition = '''# ---------------------------------------------------------------------------
# Toolbox blast-radius split (external review, closed 2026-09-02): Oracle
# keeps using get_sync_client() below completely unchanged -- still the
# single VM-pinned Toolbox instance, still required by the O5LOGON
# workaround documented in mcp_toolbox.tf. AlloyDB and MySQL now resolve a
# SEPARATE client pointed at TOOLBOX_URL_ALLOYDB_MYSQL (the Cloud Run
# Toolbox instance in mcp_toolbox_cloudrun.tf) instead, so an Oracle VM
# outage can no longer blind their telemetry too -- see
# get_sync_client_for()'s docstring below for the routing rule.
_SPLIT_TOOLBOX_ENGINES = {"alloydb", "mysql"}
_toolbox_client_cache = {}  # resolved URL -> ToolboxSyncClient


def get_sync_client_for(engine) -> ToolboxSyncClient:
    """Engine-aware client factory. Oracle (and any future engine not yet
    migrated to its own Toolbox instance) falls straight through to the
    original get_sync_client() below -- byte-identical behavior to before
    this split existed. AlloyDB/MySQL resolve TOOLBOX_URL_ALLOYDB_MYSQL
    instead; if that env var isn't set (e.g. mid-rollout, before
    mcp_toolbox_cloudrun.tf has been applied), this fails OPEN to the
    shared client rather than crashing a tick outright -- same
    don't-take-down-what-already-works posture as this module's other
    env-var defaults (see toolbox_url()/toolbox_requires_auth() above).
    That fail-open means a not-yet-migrated deploy keeps today's
    behavior (shared blast radius) rather than erroring; it does NOT mean
    the blast-radius fix silently no-ops forever -- once
    TOOLBOX_URL_ALLOYDB_MYSQL is set (mcp_toolbox_cloudrun.tf's Cloud Run
    URL, wired into cloudrun.tf's orchestrator env block), every AlloyDB/
    MySQL call routes through the new instance from the next cold start on.

    Clients are cached per resolved URL, not per engine -- AlloyDB and
    MySQL share one Cloud Run Toolbox instance today, so this reuses one
    connection for both rather than opening two identical ones."""
    engine_id = getattr(engine, "id", engine)
    if engine_id not in _SPLIT_TOOLBOX_ENGINES:
        return get_sync_client()

    url = os.environ.get("TOOLBOX_URL_ALLOYDB_MYSQL")
    if not url:
        return get_sync_client()

    if url not in _toolbox_client_cache:
        _toolbox_client_cache[url] = ToolboxSyncClient(url)
    return _toolbox_client_cache[url]


'''
db_new = db_src.replace(db_anchor, db_addition + db_anchor, 1)

# ---------------------------------------------------------------------
# 2. guardrail_callbacks.py -- two edits
# ---------------------------------------------------------------------
gc_path, gc_src = load("guardrail_callbacks.py")

gc_anchor_client = (
    '    def _get_db_client(self):\n'
    '        if self._db_client is None:\n'
    '            self._db_client = db_tools.get_sync_client()\n'
    '        return self._db_client\n'
)
verify_once(gc_src, gc_anchor_client, "guardrail_callbacks.py (_get_db_client)")
gc_replacement_client = (
    '    def _get_db_client(self, engine=None):\n'
    '        """engine=None keeps the original shared-client behavior (used\n'
    '        by any caller that predates the Toolbox split); a real engine\n'
    '        routes to db_tools.get_sync_client_for(engine), which is Oracle-\n'
    '        transparent (same client as before) and AlloyDB/MySQL-split (the\n'
    '        new Cloud Run Toolbox instance) -- see that function\'s docstring."""\n'
    '        if engine is not None:\n'
    '            return db_tools.get_sync_client_for(engine)\n'
    '        if self._db_client is None:\n'
    '            self._db_client = db_tools.get_sync_client()\n'
    '        return self._db_client\n'
)

gc_anchor_snapshot = (
    "        engine = db_registry.get_engine(action.engine)\n"
    "        try:\n"
    "            pre_state = db_tools.describe_state(self._get_db_client(), engine)\n"
)
verify_once(gc_src, gc_anchor_snapshot, "guardrail_callbacks.py (_snapshot db_tools.describe_state call)")
gc_replacement_snapshot = (
    "        engine = db_registry.get_engine(action.engine)\n"
    "        try:\n"
    "            pre_state = db_tools.describe_state(self._get_db_client(engine), engine)\n"
)

gc_new = gc_src.replace(gc_anchor_client, gc_replacement_client, 1)
gc_new = gc_new.replace(gc_anchor_snapshot, gc_replacement_snapshot, 1)

# ---------------------------------------------------------------------
# 3. pipeline.py -- one edit: each AdkOrchestrator asks for ITS OWN
#    engine's client instead of the shared default.
# ---------------------------------------------------------------------
pipe_path, pipe_src = load("pipeline.py")
pipe_anchor = "        self._db_client = db_tools.get_sync_client()\n"
verify_once(pipe_src, pipe_anchor, "pipeline.py (_db_client construction)")
pipe_replacement = "        self._db_client = db_tools.get_sync_client_for(self.engine)\n"
pipe_new = pipe_src.replace(pipe_anchor, pipe_replacement, 1)

# ---------------------------------------------------------------------
# 4. main.py -- three edits: pass db_id through to state() instead of
#    always reading the DEFAULT engine's client for a possibly-different
#    requested engine's tools.
# ---------------------------------------------------------------------
main_path, main_src = load("main.py")

main_edits = [
    (
        "        return jsonify(**db_tools.describe_database(state()._db_client, engine=engine))",
        "        return jsonify(**db_tools.describe_database(state(db_id)._db_client, engine=engine))",
        "main.py (/database/info)",
    ),
    (
        "        return jsonify(**db_tools.statspack_summary(state()._db_client, engine=engine))",
        "        return jsonify(**db_tools.statspack_summary(state(db_id)._db_client, engine=engine))",
        "main.py (/statspack/summary)",
    ),
    (
        "    result = db_tools.check_health(state()._db_client, engine)",
        "    result = db_tools.check_health(state(db_id)._db_client, engine)",
        "main.py (/database/health)",
    ),
]
for old, new, label in main_edits:
    verify_once(main_src, old, label)

main_new = main_src
for old, new, _label in main_edits:
    main_new = main_new.replace(old, new, 1)

# ---------------------------------------------------------------------
# All anchors verified -- now back up and write every file.
# ---------------------------------------------------------------------
for path, original, new_content in [
    (db_path, db_src, db_new),
    (gc_path, gc_src, gc_new),
    (pipe_path, pipe_src, pipe_new),
    (main_path, main_src, main_new),
]:
    backup_path = path + ".bak.pretoolboxsplit"
    with open(backup_path, "w") as f:
        f.write(original)
    with open(path, "w") as f:
        f.write(new_content)
    print(f"OK: patched {path} (backup at {backup_path})")

print("\nAll 4 files patched successfully.")
print("Next: split_toolbox_config.py to build the new instance's tools.yaml,")
print("then mcp_toolbox_cloudrun.tf to deploy it -- see")
print("gcp_deploy/tools_db/README_TOOLBOX_SPLIT.md for the full sequence.")
