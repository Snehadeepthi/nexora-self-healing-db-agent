"""
main.py -- GCP production entrypoint
Flask entrypoint for the deployed Self-Healing DB Agent, now built on ADK +
MCP Toolbox for Databases. Same routes, same shapes as before this rebuild
(README_DEPLOY.md's curl commands and cloudrun.tf's health/scheduler wiring
don't need to change) -- what changed is what's behind them: pipeline.py's
AdkOrchestrator (copied byte-identical from ../../../adk_agent/, see that
package for the full architecture writeup) wired here against the REAL GCP
backends instead of the local in-memory stand-ins:
  - gcp_audit.BigQueryAuditLog        instead of audit.AuditLog
  - gcp_runbooks.BigQueryRunbookStore instead of runbooks.RunbookStore
  - gcp_notifications.SlackNotifier   instead of notifications.EmailNotifier
pipeline.py never imports any of these directly -- they're constructor-
injected, which is what let it move here completely unchanged.

The orchestrator no longer talks to Oracle directly at all (that's
mcp_toolbox.tf's separate Cloud Run service's job now) -- db_tools.py's
McpToolset/ToolboxSyncClient calls TOOLBOX_URL instead, with an OIDC
identity token attached (TOOLBOX_REQUIRE_AUTH=true) the same way the Cloud
Scheduler -> this service call is already authenticated.

Known limitation, carried over unchanged from before this rebuild:
predict.py's consecutive-anomaly counter, guardrail_callbacks.py's
pending_approvals queue, and the ADK session state all live in this
process's memory. terraform/cloudrun.tf pins min_instance_count = 1 and
max_instance_request_concurrency = 1 so this stays a single warm, serialized
instance for the life of a demo session -- but a maintenance-triggered
instance recycle would still reset that in-memory state mid-incident.
"""
import logging
import os
import time
import requests

from flask import Flask, jsonify, request, send_from_directory

import db_registry
import db_tools
from gcp_audit import BigQueryAuditLog
from gcp_notifications import SlackNotifier
from gcp_runbooks import BigQueryRunbookStore
from gcp_secrets import get_secret
from pipeline import AdkOrchestrator, build_orchestrators

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("self-healing-agent")

app = Flask(__name__)

_orchestrators = None  # cold-start singleton, keyed by engine id -- see module docstring


def _bootstrap() -> dict:
    project = os.environ["GCP_PROJECT"]
    logger.info("Cold start: wiring up GCP-backed ADK pipeline for project %s", project)

    slack_webhook_url = get_secret(os.environ["SLACK_WEBHOOK_SECRET"], project)

    orchestrators = build_orchestrators(
        audit_log=BigQueryAuditLog(project_id=project),
        runbook_store=BigQueryRunbookStore(project_id=project),
        notifier=SlackNotifier(webhook_url=slack_webhook_url),
    )
    logger.info("Cold start complete -- engines: %s", ", ".join(orchestrators))
    return orchestrators


def state(db_id=None) -> AdkOrchestrator:
    global _orchestrators
    if _orchestrators is None:
        _orchestrators = _bootstrap()
    db_id = db_id or db_registry.DEFAULT_ENGINE
    return _orchestrators[db_id]


@app.route("/")
def dashboard():
    """Serves the operator dashboard (static/dashboard.html) -- a plain
    HTML/JS page that calls this same service's own JSON routes via
    same-origin fetch(). No build step, no separate frontend deploy: it
    ships inside this same container image (Dockerfile's `COPY . .`
    already picks up static/) and is served by this same Flask app, so
    opening the Cloud Run URL in a browser shows something instead of a
    404 -- this app never had a route for "/" before, which is exactly
    why a bare visit to the URL 404'd."""
    return send_from_directory(app.static_folder, "dashboard.html")


@app.route("/health")
def health():
    return jsonify(status="ok")


LAST_TICK_RESULT = {}


@app.route("/tick", methods=["POST"])
def tick():
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        result = state(db_id).run_cycle()
    except KeyError as e:
        return jsonify(error=str(e)), 400
    LAST_TICK_RESULT[db_id] = {"result": result, "ts": time.time()}
    return jsonify(**result)


@app.route("/status/last-tick")
def last_tick():
    """Lightweight polling target for the dashboard's Agent Execution Loop
    panel. Returns the most recent /tick result for the given engine --
    whichever caller triggered it, the real Cloud Scheduler heartbeat
    (every 60s, no browser involved) or the manual 'Run Tick Now' button --
    so the dashboard can show live progress of the actual autonomous loop
    instead of only reacting to its own button clicks."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    entry = LAST_TICK_RESULT.get(db_id)
    if not entry:
        return jsonify(result=None, ts=None, seconds_ago=None)
    return jsonify(result=entry["result"], ts=entry["ts"], seconds_ago=time.time() - entry["ts"])


@app.route("/approve/<incident_id>", methods=["POST"])
def approve(incident_id):
    result = state().approve_and_resolve(incident_id)
    return jsonify(status=result["status"], detail=result.get("detail"))


@app.route("/rollback/<incident_id>", methods=["POST"])
def rollback(incident_id):
    """Undo a previously-executed action for this incident, where the
    action is genuinely reversible. Currently only
    alloydb_set_statement_timeout qualifies -- its pre_state.statement_timeout
    is captured verbatim at execution time (see pipeline.py's
    _apply_statement_timeout_followup) and can be replayed exactly to
    restore the prior setting. Every other allowlisted action (session
    kills, cache flush) has no real inverse -- this returns an honest
    NOT_REVERSIBLE response for those rather than faking an undo."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    incidents = state(db_id).audit_log.incident_history(engine=db_id, limit=100)
    target = next((inc for inc in incidents if inc.get("incident_id") == incident_id), None)
    if not target:
        return jsonify(status="NOT_FOUND", detail="Incident not found in recent history."), 404
    rollback_event = next(
        (e for e in target.get("events", [])
         if e.get("event_type") == "action_executed"
         and (e.get("detail") or {}).get("action_key") == "alloydb_set_statement_timeout"),
        None,
    )
    if not rollback_event:
        return jsonify(
            status="NOT_REVERSIBLE",
            detail=("No reversible action recorded for this incident -- only "
                    "alloydb_set_statement_timeout can currently be rolled back; "
                    "session-kill and cache-flush actions have no real inverse."),
        ), 200
    prior_value = rollback_event["detail"]["snapshot"]["pre_state"]["statement_timeout"]
    try:
        db_tools.run_remediation_directly(
            state(db_id)._db_client, "alloydb_set_statement_timeout",
            {"statement_timeout_value": prior_value},
        )
    except Exception as e:
        return jsonify(status="ROLLBACK_FAILED", detail=str(e)), 502
    state(db_id).audit_log.log(
        "action_rolled_back", incident_id=incident_id, action_key="alloydb_set_statement_timeout",
        detail=f"statement_timeout restored to {prior_value}",
    )
    return jsonify(status="ROLLED_BACK", detail=f"statement_timeout restored to {prior_value}")


@app.route("/compliance")
def compliance():
    return jsonify(state().audit_log.compliance_summary())


@app.route("/incidents/history")
def incidents_history():
    """Backs the dashboard's incident-history panel -- unlike
    /approvals/pending (open Tier 3 only) and the dashboard's client-side
    renderIncidents(), this reconstructs RESOLVED incidents too straight
    from the durable BigQuery audit trail, since a fast Tier 1/2 auto-
    remediation is otherwise invisible the moment it closes."""
    db_id = request.args.get("db")
    limit = int(request.args.get("limit", 20))
    incidents = state().audit_log.incident_history(engine=db_id, limit=limit)
    return jsonify(incidents=incidents)


@app.route("/telemetry/history")
def telemetry_history():
    """Backs the dashboard's telemetry mini-charts -- see pipeline.py's
    get_telemetry_history() for the in-memory ring buffer this reads."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        readings = state(db_id).get_telemetry_history()
    except KeyError as e:
        return jsonify(error=str(e)), 400
    return jsonify(readings=readings)


@app.route("/database/info")
def database_info():
    """Backs the dashboard's database name/version tile -- a real,
    live-queried v$instance/v$version read (db_tools.describe_database),
    not hardcoded, via the same ToolboxSyncClient the Sense stage already
    uses. Returns 502 with the raw error if the grant hasn't been applied
    yet or the VM/Toolbox is unreachable, same failure shape as every other
    Toolbox-backed route here.
    Accepts ?db=<id> (default db_registry.DEFAULT_ENGINE) so the dashboard's
    database dropdown (task #30) can request any registered engine; 400 on
    an unknown id rather than silently falling back to Oracle."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        engine = db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    try:
        return jsonify(**db_tools.describe_database(state(db_id)._db_client, engine=engine))
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.route("/statspack/summary")
def statspack_summary():
    """Backs the dashboard's Statspack Snapshot panel -- see db_tools.py's
    statspack_summary() for the three Toolbox calls (statspack_window,
    statspack_summary, statspack_top_waits) this wraps.
    Accepts ?db=<id> like /database/info; engines with supports_statspack
    False (everything except Oracle for now) return the same empty shape
    the "fewer than two snapshots yet" case already returns, tagged
    unsupported=True, rather than erroring on tools that don't exist for
    that engine."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        engine = db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    if not engine.supports_statspack:
        return jsonify(window={}, ratios={}, top_wait_events=[], unsupported=True)
    try:
        return jsonify(**db_tools.statspack_summary(state(db_id)._db_client, engine=engine))
    except Exception as e:
        return jsonify(error=str(e)), 502


@app.route("/databases")
def databases():
    """Backs the dashboard's database dropdown (task #30) -- the list of
    registered engines, which optional panels each one supports, and which
    id the dropdown should default to."""
    return jsonify(
        databases=[
            {"id": e.id, "label": e.label, "supports_statspack": e.supports_statspack}
            for e in db_registry.DB_REGISTRY.values()
        ],
        default=db_registry.DEFAULT_ENGINE,
    )
@app.route("/database/health")
def database_health():
    """Real end-to-end health per engine (Risk: /health above is a static
    process-liveness check with no DB call at all -- fine as Cloud Run's own
    liveness probe, wrong as the dashboard's per-database "Healthy"
    indicator). Actually calls through Toolbox to the database with a short
    timeout and reports healthy/degraded/unreachable based on whether that
    call succeeds, not on whether this Flask process is up."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        engine = db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    result = db_tools.check_health(state(db_id)._db_client, engine)
    return jsonify(engine=engine.id, **result)
@app.route("/approvals/pending")
def approvals_pending():
    """Backs the dashboard's pending-approvals list -- reads the same
    guardrails.pending_approvals dict approve_and_resolve() pops from, so
    the list always matches what /approve/<incident_id> can actually act
    on."""
    orch = state()
    pending = [
        {
            "incident_id": incident_id,
            "action_key": p["action_key"],
            "params": p["params"],
            "query_text": p.get("query_text", ""),
        }
        for incident_id, p in orch.guardrails.pending_approvals.items()
    ]
    return jsonify(pending=pending)


@app.route("/suppressions")
def suppressions_list():
    """Tier 3 flooding guard: after an unresolved Tier 3 proposal times
    out (guardrail_callbacks.py's expire_stale_approvals), any further
    proposal of the SAME action on the SAME engine is silently suppressed
    -- audit-logged, not re-pinged to Slack -- until an operator clears it
    here. Backs a dashboard panel the same read-only-mirror way
    /approvals/pending does."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    return jsonify(suppressed=state(db_id).guardrails.list_suppressed(db_id))


@app.route("/suppressions/clear/<action_key>", methods=["POST"])
def suppressions_clear(action_key):
    """Manual reset for the Tier 3 flooding guard above -- an operator
    calls this once they've confirmed the underlying condition is
    actually handled (or was a false alarm); nothing in this process
    clears a suppression on its own, by design (see
    guardrail_callbacks.py's clear_suppression docstring)."""
    db_id = request.args.get("db", db_registry.DEFAULT_ENGINE)
    try:
        db_registry.get_engine(db_id)
    except KeyError as e:
        return jsonify(error=str(e)), 400
    cleared = state(db_id).guardrails.clear_suppression(db_id, action_key)
    return jsonify(cleared=[{"engine": e, "action_key": a} for e, a in cleared])


@app.route("/demo/trigger-tier3", methods=["POST"])
def demo_trigger_tier3():
    """Forces a Tier 3 action directly through the same before_tool gate a
    real incident would hit, skipping Sense/Predict/Reason -- exactly the
    shortcut the pre-ADK version of this endpoint used, and for the same
    documented reason: skips waiting for real Oracle telemetry to
    organically produce a memory-pressure-shaped incident. The gate
    downstream is identical either way -- it never auto-executes a Tier 3
    action, sends a real Slack approval request, and writes a real
    BigQuery audit entry.

    Targets increase_pga_target rather than restart_listener: both are
    Tier 3 in config.ALLOWLIST, but restart_listener is an OS-level action
    with no real execution path from this process (see
    db_tools.restart_listener's docstring) -- approving it always,
    correctly, ends in REJECTED. increase_pga_target is a real SQL action
    Toolbox can genuinely execute, so approving THIS demo incident shows a
    full, honest EXECUTED result end-to-end instead."""
    import uuid

    orch = state()
    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"

    class _Ctx:
        state = {"incident_id": incident_id, "query_text": "PGA memory pressure -- ORA-04036 risk rising"}

    class _Tool:
        name = "increase_pga_target"

    result = orch.guardrails.before_tool(_Tool(), {"target_mb": 300}, _Ctx())
    return jsonify(status=result["status"], detail=result.get("detail"), incident_id=incident_id)


@app.route("/demo/trigger-tier2", methods=["POST"])
def demo_trigger_tier2():
    """Companion to /demo/trigger-tier3, same honest shortcut -- skips
    Sense/Predict/Reason and exercises the real guardrail gate directly --
    but for a Tier 2 action (flush_shared_pool), which auto-executes (no
    human approval) with just loud audit logging, rather than pausing for
    approval.

    before_tool() returning None here means "allowed, but nothing ran yet"
    (unlike Tier 3's before_tool, which short-circuits with an explicit
    PENDING_APPROVAL dict and genuinely never runs anything). The real ADK
    Runner would normally call the actual MCP tool itself right after
    before_tool() allows it through; since this shortcut skips the Runner
    entirely, it has to do that step manually -- via
    db_tools.run_remediation_directly, the same helper the Tier 3 approve()
    path already uses -- then call after_tool() to log the action_executed
    audit entry and close the breaker, exactly like a genuine Tier 2
    auto-remediation would."""
    import uuid

    orch = state()
    incident_id = f"demo-tier2-{uuid.uuid4().hex[:8]}"

    class _Ctx:
        state = {
            "incident_id": incident_id,
            "query_text": "Library cache contention -- shared pool fragmentation high parse",
        }

    class _Tool:
        name = "flush_shared_pool"

    gate_result = orch.guardrails.before_tool(_Tool(), {}, _Ctx())
    if gate_result is not None:
        # REJECTED / BREAKER_OPEN -- the real guardrail gate blocked it before anything ran
        return jsonify(status=gate_result["status"], detail=gate_result.get("detail"), incident_id=incident_id)

    try:
        raw_result = db_tools.run_remediation_directly(orch._db_client, "flush_shared_pool", {})
    except Exception as e:
        result = orch.guardrails.on_tool_error(_Tool(), {}, _Ctx(), e)
        return jsonify(status=result["status"], detail=result.get("detail"), incident_id=incident_id)

    orch.guardrails.after_tool(_Tool(), {}, _Ctx(), raw_result)
    return jsonify(status="EXECUTED", detail=raw_result, incident_id=incident_id)


@app.route("/demo/trigger-tier1", methods=["POST"])
def demo_trigger_tier1():
    """Oracle Tier 1 lock-cascade demo -- stages a REAL blocking-session
    scenario against the live Oracle instance on this same VM: one holder
    takes a row lock via SELECT ... FOR UPDATE, 9 waiters queue up behind
    it (see lock-cascade-oracle.cgi / simulate_blocking_vm.sh for the exact
    mechanism). Unlike /demo/trigger-tier2 and /demo/trigger-tier3 above,
    this does NOT take the old synthetic-incident shortcut -- it stages a
    genuinely real incident and lets the real pipeline discover it on its
    own next tick, same as the AlloyDB/MySQL demo triggers.
    active_blocked_sessions has no idle-time floor, so detection is fast --
    roughly 1-2 minutes (two consecutive polling ticks)."""
    import uuid
    incident_id = f"demo-tier1-oracle-staged-{uuid.uuid4().hex[:8]}"
    project = os.environ["GCP_PROJECT"]
    trigger_secret = get_secret(os.environ["DEMO_TRIGGER_SECRET"], project)
    trigger_url = f"{os.environ['DEMO_TRIGGER_URL']}/cgi-bin/lock-cascade-oracle.cgi"
    try:
        resp = requests.get(trigger_url, headers={"X-Trigger-Secret": trigger_secret}, timeout=30)
        resp.raise_for_status()
        staged = resp.json()
    except Exception as e:
        logger.exception("demo-trigger call to %s failed", trigger_url)
        return jsonify(status="TRIGGER_FAILED", detail=str(e), incident_id=incident_id), 502
    return jsonify(
        status="STAGED",
        detail=staged,
        incident_id=incident_id,
        message=(
            "Real lock-cascade sessions opened against Oracle (one holder, "
            "9 waiters queued behind a row lock). The real "
            "detection/remediation pipeline will pick this up on its own "
            "within ~1-2 minutes -- watch the Incidents panel."
        ),
    )


@app.route("/demo/trigger-tier2-alloydb", methods=["POST"])
def demo_trigger_tier2_alloydb():
    """AlloyDB counterpart to /demo/trigger-tier2 -- REWORKED for #52 to
    stage a REAL failure instead of faking one. The old version generated
    a synthetic incident_id/query_text and immediately forced
    run_remediation_directly() -- honest about exercising the real
    guardrail gate, but the "incident" itself was fabricated. This version
    instead calls the demo-trigger listener on the Oracle VM (a tiny
    Python CGI server, see gcp_deploy/demo_triggers/) which opens 4 real
    idle-in-transaction sessions (900s) against the live AlloyDB instance
    -- the exact same mechanism the simulate_idle_in_transaction_alloydb.sh
    script uses manually over SSH. No guardrail gate call happens here:
    the REAL Sense->Predict->Reason->Act pipeline discovers these sessions
    and runs its own real before_tool()/after_tool() calls on its next
    tick, exactly like a genuine production incident. Detection takes
    roughly 5-6 minutes (a 300-second idle floor built into the anomaly query, plus up to two polling
    ticks), a real wait instead of the old version's instant fake
    response."""
    import uuid
    incident_id = f"demo-tier2-alloydb-staged-{uuid.uuid4().hex[:8]}"
    project = os.environ["GCP_PROJECT"]
    trigger_secret = get_secret(os.environ["DEMO_TRIGGER_SECRET"], project)
    trigger_url = f"{os.environ['DEMO_TRIGGER_URL']}/cgi-bin/idle-alloydb.cgi"
    try:
        resp = requests.get(trigger_url, headers={"X-Trigger-Secret": trigger_secret}, timeout=30)
        resp.raise_for_status()
        staged = resp.json()
    except Exception as e:
        logger.exception("demo-trigger call to %s failed", trigger_url)
        return jsonify(status="TRIGGER_FAILED", detail=str(e), incident_id=incident_id), 502
    return jsonify(
        status="STAGED",
        detail=staged,
        incident_id=incident_id,
        message=(
            "Real idle-in-transaction sessions opened against AlloyDB. "
            "The real detection/remediation pipeline will pick this up on "
            "its own within ~5-6 minutes -- watch the Incidents panel."
        ),
    )


@app.route("/demo/trigger-tier3-alloydb", methods=["POST"])
def demo_trigger_tier3_alloydb():
    """AlloyDB counterpart to /demo/trigger-tier3 -- targets
    alloydb_reset_all_connections (Tier 3, requires human approval via
    Slack before it ever runs). Safe to CALL: before_tool() only ever
    short-circuits with PENDING_APPROVAL here, it never executes anything
    itself. Approving it via /approve/<incident_id> genuinely terminates
    every other session on the database -- only approve this during a
    controlled demo, same caveat as Oracle's restart_listener except this
    one is a real, successfully-executing action rather than one that
    always raises."""
    import uuid

    orch = state("alloydb")
    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"

    class _Ctx:
        state = {"incident_id": incident_id, "query_text": "AlloyDB connection storm -- last-resort reset requested"}

    class _Tool:
        name = "alloydb_reset_all_connections"

    result = orch.guardrails.before_tool(_Tool(), {}, _Ctx())
    return jsonify(status=result["status"], detail=result.get("detail"), incident_id=incident_id)


@app.route("/demo/trigger-tier1-alloydb", methods=["POST"])
def demo_trigger_tier1_alloydb():
    """AlloyDB Tier 1 lock-cascade demo -- stages a REAL blocking-session
    scenario: one holder takes a row lock via SELECT ... FOR UPDATE, 9
    waiters queue up behind it (see lock-cascade-alloydb.cgi /
    simulate_blocking_alloydb.sh for the exact mechanism). No guardrail
    gate call happens here: the REAL pipeline discovers the blocked
    sessions and runs its own real before_tool()/after_tool() calls on its
    next tick. active_blocked_sessions has no idle-time floor, so
    detection is fast -- roughly 1-2 minutes."""
    import uuid
    incident_id = f"demo-tier1-alloydb-staged-{uuid.uuid4().hex[:8]}"
    project = os.environ["GCP_PROJECT"]
    trigger_secret = get_secret(os.environ["DEMO_TRIGGER_SECRET"], project)
    trigger_url = f"{os.environ['DEMO_TRIGGER_URL']}/cgi-bin/lock-cascade-alloydb.cgi"
    try:
        resp = requests.get(trigger_url, headers={"X-Trigger-Secret": trigger_secret}, timeout=30)
        resp.raise_for_status()
        staged = resp.json()
    except Exception as e:
        logger.exception("demo-trigger call to %s failed", trigger_url)
        return jsonify(status="TRIGGER_FAILED", detail=str(e), incident_id=incident_id), 502
    return jsonify(
        status="STAGED",
        detail=staged,
        incident_id=incident_id,
        message=(
            "Real lock-cascade sessions opened against AlloyDB (one holder, "
            "9 waiters queued behind a row lock). The real "
            "detection/remediation pipeline will pick this up on its own "
            "within ~1-2 minutes -- watch the Incidents panel."
        ),
    )


@app.route("/demo/trigger-tier2-mysql", methods=["POST"])
def demo_trigger_tier2_mysql():
    """MySQL counterpart to /demo/trigger-tier2 -- REWORKED for #52 to
    stage a REAL failure instead of faking one, same reasoning as the
    AlloyDB counterpart above: calls the demo-trigger listener on the
    Oracle VM, which opens 4 real idle-in-transaction sessions (900s)
    against the live MySQL instance -- the exact same mechanism the
    simulate_idle_in_transaction_mysql.sh script uses manually over SSH.
    No guardrail gate call happens here: the REAL pipeline discovers these
    sessions and runs its own real before_tool()/after_tool() calls on its
    next tick. Detection takes roughly 5-6 minutes (a 300-second idle floor built into the anomaly query, plus up to two polling ticks)."""
    import uuid
    incident_id = f"demo-tier2-mysql-staged-{uuid.uuid4().hex[:8]}"
    project = os.environ["GCP_PROJECT"]
    trigger_secret = get_secret(os.environ["DEMO_TRIGGER_SECRET"], project)
    trigger_url = f"{os.environ['DEMO_TRIGGER_URL']}/cgi-bin/idle-mysql.cgi"
    try:
        resp = requests.get(trigger_url, headers={"X-Trigger-Secret": trigger_secret}, timeout=30)
        resp.raise_for_status()
        staged = resp.json()
    except Exception as e:
        logger.exception("demo-trigger call to %s failed", trigger_url)
        return jsonify(status="TRIGGER_FAILED", detail=str(e), incident_id=incident_id), 502
    return jsonify(
        status="STAGED",
        detail=staged,
        incident_id=incident_id,
        message=(
            "Real idle-in-transaction sessions opened against MySQL. "
            "The real detection/remediation pipeline will pick this up on "
            "its own within ~5-6 minutes -- watch the Incidents panel."
        ),
    )


@app.route("/demo/trigger-tier1-mysql", methods=["POST"])
def demo_trigger_tier1_mysql():
    """MySQL Tier 1 lock-cascade demo -- stages a REAL blocking-session
    scenario: one holder takes a row lock via SELECT ... FOR UPDATE, 10
    waiters queue up behind it (see lock-cascade-mysql.cgi /
    simulate_blocking_mysql.sh for the exact mechanism). No guardrail gate
    call happens here: the REAL pipeline discovers the blocked sessions and
    runs its own real before_tool()/after_tool() calls on its next tick.
    active_blocked_sessions has no idle-time floor, so detection is fast --
    roughly 1-2 minutes (two consecutive polling ticks), same as the
    runaway-query path."""
    import uuid
    incident_id = f"demo-tier1-mysql-staged-{uuid.uuid4().hex[:8]}"
    project = os.environ["GCP_PROJECT"]
    trigger_secret = get_secret(os.environ["DEMO_TRIGGER_SECRET"], project)
    trigger_url = f"{os.environ['DEMO_TRIGGER_URL']}/cgi-bin/lock-cascade-mysql.cgi"
    try:
        resp = requests.get(trigger_url, headers={"X-Trigger-Secret": trigger_secret}, timeout=30)
        resp.raise_for_status()
        staged = resp.json()
    except Exception as e:
        logger.exception("demo-trigger call to %s failed", trigger_url)
        return jsonify(status="TRIGGER_FAILED", detail=str(e), incident_id=incident_id), 502
    return jsonify(
        status="STAGED",
        detail=staged,
        incident_id=incident_id,
        message=(
            "Real lock-cascade sessions opened against MySQL (one holder, "
            "10 waiters queued behind a row lock). The real "
            "detection/remediation pipeline will pick this up on its own "
            "within ~1-2 minutes -- watch the Incidents panel."
        ),
    )


@app.route("/demo/trigger-tier3-mysql", methods=["POST"])
def demo_trigger_tier3_mysql():
    """MySQL counterpart to /demo/trigger-tier3 -- targets
    mysql_reset_all_connections (Tier 3, requires human approval via
    Slack before it ever runs). Safe to CALL: before_tool() only ever
    short-circuits with PENDING_APPROVAL here, it never executes anything
    itself. Approving it via /approve/<incident_id> genuinely terminates
    every other connection on the instance -- only approve this during a
    controlled demo."""
    import uuid

    orch = state("mysql")
    incident_id = f"{orch.engine.id}-demo-tier3-{uuid.uuid4().hex[:8]}"

    class _Ctx:
        state = {"incident_id": incident_id, "query_text": "MySQL connection storm -- last-resort reset requested"}

    class _Tool:
        name = "mysql_reset_all_connections"

    result = orch.guardrails.before_tool(_Tool(), {}, _Ctx())
    return jsonify(status=result["status"], detail=result.get("detail"), incident_id=incident_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
