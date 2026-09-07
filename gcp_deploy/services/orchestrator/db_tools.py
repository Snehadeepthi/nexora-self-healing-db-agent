"""
db_tools.py
All database access goes through MCP Toolbox for Databases -- this module
owns the two ways this agent ever talks to it:

  1. Deterministic Sense-stage reads (poll_telemetry, describe_state) --
     called directly and synchronously via the toolbox-core SDK, with no LLM
     involved. Polling telemetry every tick doesn't need reasoning, and
     spending a Gemini call on it would be pure Risk 6 (cost overrun) waste
     for zero benefit -- config.py's cost_guard is budgeted around the
     Reason-stage calls only, same as the original reference implementation.
  2. LLM-driven remediation actions (kill_blocking_session, flush_shared_pool,
     kill_runaway_query, increase_pga_target) -- exposed to the ADK agent as
     real MCP tools via McpToolset, connected over streamable HTTP to the
     same Toolbox server tools_db/tools.yaml declares. tool_filter restricts
     the agent's visible tool surface to exactly those four action_keys --
     it never even sees poll_telemetry/describe_state as something it could
     call, let alone raw SQL.

restart_listener is the one config.ALLOWLIST action Toolbox can't run: it's
an OS-level `lsnrctl restart`, not SQL -- Toolbox is a SQL/database tool server and has no path
to a host shell. It stays a local ADK FunctionTool in ../db_tools.py that
fails loudly with the same honest error the reference GCP deployment's
oracle_client.py already documents, rather than being faked here.

ASSUMPTION FLAGGED FOR VERIFICATION: this connects McpToolset to the same
bare Toolbox base URL the toolbox-core SDK uses (ToolboxSyncClient's default
protocol is already native MCP -- see its `protocol` kwarg default). If your
deployed Toolbox version exposes the MCP endpoint at a different path (e.g.
`/mcp`), adjust `_MCP_URL` below. This couldn't be verified against a live
Toolbox server from the sandbox this was built in -- confirm it against your
actual running instance (agent.py's docstring has the one-line check).
"""

import concurrent.futures
import json
import os
import time
from datetime import datetime, timezone

from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from toolbox_core import ToolboxSyncClient
import db_registry

REMEDIATION_TOOL_NAMES = [
    "kill_blocking_session",
    "kill_runaway_query",
    "flush_shared_pool",
    "increase_pga_target",
    "alloydb_kill_blocking_session",
    "alloydb_kill_runaway_query",
    "alloydb_terminate_idle_in_transaction",
    "alloydb_reset_all_connections",
    "mysql_kill_blocking_session",
    "mysql_kill_runaway_query",
    "mysql_terminate_idle_in_transaction",
    "mysql_reset_all_connections",
]


def toolbox_url() -> str:
    return os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000")


def _auth_required() -> bool:
    """False locally (docker-compose's Toolbox has no IAM in front of it).
    True in GCP: mcp_toolbox.tf grants only orchestrator_sa run.invoker on
    the Toolbox Cloud Run service (same IAM-private-by-default posture
    cloudrun.tf already uses for the orchestrator itself, and the same
    OIDC-token pattern the Cloud Scheduler -> orchestrator call uses) --
    the GCP orchestrator's env vars set TOOLBOX_REQUIRE_AUTH=true."""
    return os.environ.get("TOOLBOX_REQUIRE_AUTH", "false").lower() == "true"


def _fetch_id_token() -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    return fetch_id_token(Request(), toolbox_url())


def _fetch_id_token_for(url: str) -> str:
    """Same as _fetch_id_token() above, but for an arbitrary audience URL --
    used by get_sync_client_for()'s split-Toolbox path, where the audience is
    the AlloyDB/MySQL Cloud Run instance, not the Oracle VM's toolbox_url()."""
    from google.auth.transport.requests import Request
    from google.oauth2.id_token import fetch_id_token

    return fetch_id_token(Request(), url)


# ---------------------------------------------------------------------------
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
        # Reachability fix: this instance is IAM-gated now (see
        # mcp_toolbox_cloudrun.tf), so every call needs a fresh OIDC
        # identity token attached -- reusing the exact
        # fetch_id_token(Request(), <audience>) call get_sync_client()'s own
        # auth-enabled branch already uses (proven live against Toolbox
        # 1.9.0), just parameterized by URL instead of hardcoded to
        # toolbox_url() -- the audience MUST be this specific service's URL,
        # not the Oracle VM's, or Cloud Run's IAM check rejects the token.
        # A lambda (not a precomputed string) is required here, not just
        # convenient: ID tokens expire (~1hr), and this client is cached
        # and reused across many ticks, so the header has to be
        # recomputed on every single call rather than fixed at
        # construction time.
        headers = {"Authorization": lambda u=url: f"Bearer {_fetch_id_token_for(u)}"}
        _toolbox_client_cache[url] = ToolboxSyncClient(url, client_headers=headers)
    return _toolbox_client_cache[url]


def get_sync_client() -> ToolboxSyncClient:
    """One of these per process: used for the deterministic Sense-stage
    reads, and for the Tier 3 approve() path (which executes the
    already-proposed action directly, bypassing the LLM entirely -- see
    pipeline.py's approve_and_resolve(), which mirrors act.py's approve()
    calling self._run() directly rather than re-asking reason.py)."""
    if _auth_required():
        headers = {"Authorization": lambda: f"Bearer {_fetch_id_token()}"}
        return ToolboxSyncClient(toolbox_url(), client_headers=headers)
    return ToolboxSyncClient(toolbox_url())


def _first_row(raw_result: str) -> dict:
    """Toolbox SQL tools return a JSON-encoded string of result rows -- but
    NOT always wrapped in an array: confirmed live against Toolbox 1.9.0
    that a single-row oracle-sql result serializes as a bare JSON object
    (e.g. '{"COL":val}'), not a one-element array (e.g. '[{"COL":val}]') as
    originally assumed here. A prior version of this function did
    `json.loads(raw_result)[0]`, which on a bare dict indexes it with the
    integer key 0 -- raising KeyError(0), whose str() is just "0" and reads
    like nothing at all in an error message. Handle both shapes so this
    keeps working if a future Toolbox release reintroduces the array form."""
    parsed = json.loads(raw_result)
    if isinstance(parsed, list):
        if not parsed:
            raise RuntimeError("query returned no rows")
        return parsed[0]
    if isinstance(parsed, dict):
        return parsed
    raise RuntimeError(f"unexpected result shape from Toolbox: {type(parsed).__name__}: {raw_result!r}")


def _all_rows(raw_result: str) -> list:
    """Like _first_row, but for tools that return multiple rows (e.g.
    statspack_top_waits) -- Toolbox serializes a multi-row oracle-sql
    result as a JSON array; a single-row result can still arrive as a bare
    dict (see _first_row's docstring), so that case is normalized to a
    one-element list here too."""
    parsed = json.loads(raw_result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    raise RuntimeError(f"unexpected result shape from Toolbox: {type(parsed).__name__}: {raw_result!r}")


def _load_row(raw_result: str, tool_name: str) -> dict:
    try:
        return _first_row(raw_result)
    except Exception as e:
        raise RuntimeError(f"{tool_name} returned unparseable result: {raw_result!r} ({e})") from e


def _load_rows(raw_result: str, tool_name: str) -> list:
    try:
        return _all_rows(raw_result)
    except Exception as e:
        raise RuntimeError(f"{tool_name} returned unparseable result: {raw_result!r} ({e})") from e


def _with_retries(fn, attempts=3, base_delay_seconds=0.5, retry_on=(Exception,)):
    """Reliability safeguard: retries a flaky Toolbox/Oracle READ with
    exponential backoff (0.5s, 1s, ...) so a single transient blip -- a
    brief network hiccup, Oracle mid-restart, a listener registration race
    like kb-03 in the Knowledge Base -- doesn't immediately fail an entire
    tick with "oracle_unreachable". Deliberately used ONLY for read-only
    calls (poll_telemetry, describe_state) -- never for remediation actions
    (kill_blocking_session, flush_shared_pool, etc.), which must fail loudly
    on the first error rather than risk a KILL SESSION or ALTER SYSTEM
    statement being silently retried against unclear post-failure state."""
    last_exc = None
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on as e:
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(base_delay_seconds * (2 ** attempt))
    raise last_exc


_HEALTH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="health-check")
_HEALTH_CHECK_TIMEOUT_SECONDS = 3
def check_health(client: ToolboxSyncClient, engine) -> dict:
    """Real end-to-end health probe for the dashboard's per-database
    'Healthy' indicator (main.py's /database/health) -- unlike the old
    /health route (a static jsonify(status="ok") with no DB call at all),
    this actually calls through Toolbox to the database and reports
    healthy/degraded/unreachable based on whether that call succeeds.
    Deliberately NOT wrapped in _with_retries -- a health check needs to
    reflect the current instant, not paper over a real outage behind
    several seconds of backoff.
    Timeout-guarded via a shared ThreadPoolExecutor: ToolboxSyncClient
    doesn't expose a per-call timeout, and a truly hung connection (vs. a
    clean error) would otherwise block this request indefinitely. NOTE:
    Python threads can't be forcibly killed -- on a genuine hang this still
    returns "degraded" to the caller after _HEALTH_CHECK_TIMEOUT_SECONDS,
    but the background thread keeps running until the underlying call
    eventually errors or times out on its own. Bounded by max_workers=4."""
    tool_key = db_registry.tool_name(engine, engine.health_tool)
    start = time.time()
    try:
        tool = client.load_tool(tool_key)
        future = _HEALTH_EXECUTOR.submit(tool)
        future.result(timeout=_HEALTH_CHECK_TIMEOUT_SECONDS)
        return {"status": "healthy", "latency_ms": int((time.time() - start) * 1000), "detail": None}
    except concurrent.futures.TimeoutError:
        return {
            "status": "degraded",
            "latency_ms": int((time.time() - start) * 1000),
            "detail": f"no response within {_HEALTH_CHECK_TIMEOUT_SECONDS}s",
        }
    except Exception as e:
        return {"status": "unreachable", "latency_ms": int((time.time() - start) * 1000), "detail": str(e)}
def poll_telemetry(client: ToolboxSyncClient, engine=None) -> dict:
    """Sense stage (Step 3): matches oracle_client.py's OracleClient.poll()
    return shape exactly (including the tick/ts stamps, which -- same as
    the original -- are added here in Python rather than by the query),
    so sense.py and predict.py (both copied unchanged) need no changes.

    cpu_utilization_pct defaults to 0 when Oracle's v$sysmetric hasn't
    produced a "Host CPU Utilization (%)" reading yet -- confirmed live
    that this returns SQL NULL (not the 0 the query's own NVL(...,0)
    should guarantee) shortly after a fresh instance start, before the
    metrics-collection background job has run its first pass. Coerced here
    rather than chased further in the SQL: a temporarily-missing CPU
    reading isn't itself an outage worth surfacing as one.

    Wrapped in _with_retries: a transient failure here would otherwise
    surface as a whole-tick "oracle_unreachable", which -- now that
    pipeline.py's Sense stage also shares the oracle_db circuit breaker --
    would count toward tripping the breaker on a blip that would have
    resolved itself a second later."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    tool = client.load_tool(db_registry.tool_name(engine, "poll_telemetry"))
    row = _first_row(_with_retries(tool))
    row = {k.upper(): v for k, v in row.items()}
    cpu = row["CPU_UTILIZATION_PCT"]
    reading = {
        "tick": int(time.time()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "active_blocked_sessions": int(row["ACTIVE_BLOCKED_SESSIONS"]),
        "cpu_utilization_pct": int(cpu) if cpu is not None else 0,
    }
    # AlloyDB-only signals -- present only when the underlying SQL returns
    # them (Oracle's poll_telemetry tool has no equivalent columns, so
    # these are simply absent from an Oracle reading; predict.py's
    # AnomalyDetector treats a missing key as "not a candidate", never an
    # error -- see its module docstring).
    if "MAX_QUERY_DURATION_SECONDS" in row:
        reading["max_query_duration_seconds"] = int(row["MAX_QUERY_DURATION_SECONDS"] or 0)
    if "IDLE_IN_TRANSACTION_COUNT" in row:
        reading["idle_in_transaction_count"] = int(row["IDLE_IN_TRANSACTION_COUNT"] or 0)
    if "TOTAL_CONNECTIONS" in row and "MAX_CONNECTIONS" in row:
        total = int(row["TOTAL_CONNECTIONS"] or 0)
        cap = int(row["MAX_CONNECTIONS"] or 0) or 1
        reading["connection_pct"] = round(total / cap, 4)
    return reading


def describe_state(client: ToolboxSyncClient, engine=None) -> dict:
    """Act-stage pre-action snapshot (Risk 1): matches
    RealDbExecutor.describe_state()'s return shape. Also wrapped in
    _with_retries -- see poll_telemetry's docstring; a flaky snapshot read
    shouldn't block a real remediation from proceeding.

    engine defaults to Oracle for backward compatibility with existing
    callers. Row keys are normalized to uppercase before lookup -- same fix
    as describe_database(): Oracle's driver uppercases unquoted column
    names, Postgres/AlloyDB preserves the lowercase alias written in
    tools.yaml, so a single hardcoded casing convention silently breaks one
    engine or the other."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    tool = client.load_tool(db_registry.tool_name(engine, "describe_state"))
    row = _first_row(_with_retries(tool))
    row = {k.upper(): v for k, v in row.items()}
    return {"active_blocked_sessions_snapshot": int(row["ACTIVE_BLOCKED_SESSIONS_SNAPSHOT"])}


def get_statement_timeout(client: ToolboxSyncClient, engine=None) -> str:
    """AlloyDB-only: reads the database's current statement_timeout as raw
    text (e.g. '0', '5000ms') immediately before
    alloydb_set_statement_timeout changes it, so the exact prior value can
    be replayed verbatim on rollback -- see main.py's /rollback endpoint
    and pipeline.py's _apply_statement_timeout_followup."""
    engine = engine or db_registry.get_engine("alloydb")
    tool = client.load_tool("alloydb_get_statement_timeout")
    row = _first_row(_with_retries(tool))
    row = {k.upper(): v for k, v in row.items()}
    return str(row["STATEMENT_TIMEOUT"])


def find_blocking_session(client: ToolboxSyncClient, engine=None) -> dict:
    """Looks up the real sid/serial# of the session currently HOLDING a
    blocking lock -- kill_blocking_session's param_schema requires exactly
    these two values, and nothing else in the Sense/Predict event gives the
    LLM a grounded way to know them (the incident prompt only ever carried
    metric/status/value/query_text before this). Called from pipeline.py
    right after an anomaly is confirmed, and the result is embedded directly
    into the incident prompt as a stated fact -- not exposed to the LLM as a
    callable tool, so it can't be skipped or mis-called. See pipeline.py's
    _lookup_blocking_session for the full rationale."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    tool = client.load_tool(db_registry.tool_name(engine, "find_blocking_session"))
    row = _first_row(tool())
    row = {k.upper(): v for k, v in row.items()}
    if engine.id == "alloydb":
        return {"pid": int(row["BLOCKING_PID"])}
    if engine.id == "mysql":
        return {"processlist_id": int(row["BLOCKING_PROCESSLIST_ID"])}
    return {"sid": int(row["BLOCKING_SID"]), "serial": int(row["BLOCKING_SERIAL"])}


def find_runaway_query(client: ToolboxSyncClient, engine=None) -> dict:
    """AlloyDB analog of find_blocking_session: grounds
    alloydb_kill_runaway_query's required pid param in the real longest-
    running active query's backend pid, instead of leaving the LLM to
    guess it -- same anti-hallucination rationale, see
    find_blocking_session's docstring. AlloyDB-only: Oracle has no
    kill_runaway_query action requiring a grounded pid this way."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    tool = client.load_tool(db_registry.tool_name(engine, "find_runaway_query"))
    row = _first_row(tool())
    row = {k.upper(): v for k, v in row.items()}
    if engine.id == "mysql":
        return {"processlist_id": int(row["RUNAWAY_PROCESSLIST_ID"])}
    return {"pid": int(row["RUNAWAY_PID"])}


def describe_database(client: ToolboxSyncClient, engine=None) -> dict:
    """Dashboard-only info tile (main.py's /database/info): the real
    instance name, current container (PDB) name, and Oracle version
    banner -- read live from v$instance/v$version, not hardcoded, so this
    reflects whatever's actually running rather than going stale if
    database.tf's Oracle image tag ever changes. Needs
    V_$INSTANCE/V_$VERSION grants on top of the V_$SESSION/V_$SYSMETRIC
    database.tf's grant script already applies -- see its grant_v_views.sql
    heredoc, which now also grants these two."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    tool = client.load_tool(db_registry.tool_name(engine, "describe_database"))
    row = _first_row(tool())
    # Oracle's driver uppercases unquoted column names; Postgres/AlloyDB
    # preserves the lowercase aliases written in tools.yaml -- normalize
    # once here so the same .get(...) calls work for both engines instead
    # of hardcoding one casing convention.
    row = {k.upper(): v for k, v in row.items()}
    return {
        "db_name": row.get("DB_NAME"),
        "pdb_name": row.get("PDB_NAME"),
        "version_short": row.get("VERSION_SHORT"),
        "version_banner": row.get("VERSION_BANNER"),
    }


def statspack_summary(client: ToolboxSyncClient, engine=None) -> dict:
    """Dashboard-only Statspack Snapshot panel (main.py's
    /statspack/summary): core instance-efficiency ratios, load-profile
    rates, and top wait events computed live between the two latest
    Statspack snapshots. Three Toolbox round trips -- statspack_window
    first (no parameters) to find which snapshot IDs to ask about, then
    statspack_summary and statspack_top_waits parameterized with them.

    Wrapped in _with_retries like the other dashboard/Sense-stage reads:
    this is read-only and polled on a timer, so a transient blip shouldn't
    surface as a dashboard error.

    Returns an empty-ish shape (not an exception) when fewer than two
    snapshots exist yet -- e.g. right after Statspack was first installed --
    since "no data yet" isn't itself a failure worth a 502.

    Uses _load_row/_load_rows (not _first_row/_all_rows directly) so a
    malformed Toolbox result says exactly which of the three calls failed
    and what it actually returned, instead of a generic JSON decode error
    with no context."""
    engine = engine or db_registry.get_engine(db_registry.DEFAULT_ENGINE)
    window_tool = client.load_tool(db_registry.tool_name(engine, "statspack_window"))
    window_row = _load_row(_with_retries(window_tool), "statspack_window")
    begin_snap = window_row.get("BEGIN_SNAP")
    end_snap = window_row.get("END_SNAP")

    if begin_snap is None or end_snap is None or begin_snap == end_snap:
        return {"window": {}, "ratios": {}, "top_wait_events": []}

    summary_tool = client.load_tool(db_registry.tool_name(engine, "statspack_summary"))
    summary_row = _load_row(
        _with_retries(lambda: summary_tool(begin_snap=begin_snap, end_snap=end_snap)),
        "statspack_summary",
    )

    waits_tool = client.load_tool(db_registry.tool_name(engine, "statspack_top_waits"))
    wait_rows = _load_rows(
        _with_retries(lambda: waits_tool(begin_snap=begin_snap, end_snap=end_snap)),
        "statspack_top_waits",
    )

    def _f(v):
        return float(v) if v is not None else None

    return {
        "window": {
            "begin_snap": int(begin_snap),
            "end_snap": int(end_snap),
            "elapsed_minutes": _f(summary_row.get("ELAPSED_MINUTES")),
        },
        "ratios": {
            "buffer_cache_hit_pct": _f(summary_row.get("BUFFER_CACHE_HIT_PCT")),
            "library_cache_hit_pct": _f(summary_row.get("LIBRARY_CACHE_HIT_PCT")),
            "redo_per_sec_kb": _f(summary_row.get("REDO_PER_SEC_KB")),
            "logical_reads_per_sec": _f(summary_row.get("LOGICAL_READS_PER_SEC")),
            "physical_reads_per_sec": _f(summary_row.get("PHYSICAL_READS_PER_SEC")),
            "executes_per_sec": _f(summary_row.get("EXECUTES_PER_SEC")),
        },
        "top_wait_events": [
            {
                "event": r.get("EVENT"),
                "waits": int(r["WAITS"]) if r.get("WAITS") is not None else None,
                "time_waited_sec": _f(r.get("TIME_WAITED_SEC")),
            }
            for r in wait_rows
        ],
    }


def run_remediation_directly(client: ToolboxSyncClient, action_key: str, params: dict) -> str:
    """Executes an allowlisted remediation tool directly, without going
    through the LLM agent at all -- the Tier 3 human-approval path only.
    Every guardrail that gated the ORIGINAL proposal (allowlist signoff,
    param_schema validation) already ran once in guardrail_callbacks.py
    before this incident was ever queued as pending_approval; this call
    does not re-run the LLM or re-validate, exactly like act.py.approve()."""
    tool = client.load_tool(action_key)
    return tool(**params)


def restart_listener(listener_name: str) -> dict:
    """Tier 3 action with no execution path from this process to the DB
    host's OS. See gcp_deploy/services/orchestrator/oracle_client.py's
    docstring for the same documented limitation and how to close it (SSH
    via OS Login, or a small on-VM control agent). Fails loudly rather than
    silently pretending this succeeded -- guardrail_callbacks.py's
    on_tool_error_callback logs this as a real action_failed audit event."""
    raise RuntimeError(
        f"restart_listener('{listener_name}') is an OS-level action with no "
        f"execution path from this process to the DB host. Wire up SSH (OS "
        f"Login) or an on-VM control agent before relying on this in "
        f"production -- see oracle_client.py's docstring."
    )


restart_listener_tool = FunctionTool(restart_listener)


def _mcp_header_provider(_readonly_context):
    return {"Authorization": f"Bearer {_fetch_id_token()}"}


def load_remediation_toolset() -> McpToolset:
    """The ADK agent's only path to the database -- an MCP connection to the
    Toolbox server, scoped via tool_filter to exactly the four SQL
    remediation actions in config.ALLOWLIST. It never sees the read tools,
    let alone raw SQL, as something it could call.

    ASSUMPTION FLAGGED FOR VERIFICATION above (now confirmed, live, against
    Toolbox 1.9.0) resolved the wrong way: McpToolset does NOT append a
    default path to the base URL the way ToolboxSyncClient apparently does
    internally -- it POSTs to the bare TOOLBOX_URL as given. Toolbox's own
    real MCP endpoint is at `/mcp/` (confirmed via its own access logs:
    every one of ToolboxSyncClient's successful poll_telemetry/
    describe_state/find_blocking_session calls goes to `POST /mcp/`, while
    McpToolset without this suffix got a flat `405 Method Not Allowed` on
    every attempt). That failure is silent from the model's point of view --
    McpToolset just logs a WARNING and the agent ends up with zero tools, so
    every incident harmlessly-looking resolves to "no tool call made"
    (pipeline.py's no_action_proposed) instead of surfacing as an error.
    Only caught because the organic Sense->Predict->Reason->Act path was
    actually exercised end-to-end for the first time; every previous
    successful demo used a guardrail-gate shortcut
    (/demo/trigger-tier2, /demo/trigger-tier3) that bypasses the LLM agent
    entirely and never touched this connection."""
    kwargs = dict(
        connection_params=StreamableHTTPConnectionParams(url=f"{toolbox_url().rstrip('/')}/mcp/"),
        tool_filter=REMEDIATION_TOOL_NAMES,
    )
    if _auth_required():
        kwargs["header_provider"] = _mcp_header_provider
    return McpToolset(**kwargs)
