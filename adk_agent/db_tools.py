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
an OS-level `lsnrctl restart`, not SQL, so it isn't in tools.yaml at all. It
is instead a plain local ADK FunctionTool that fails with the same honest
error gcp_deploy/services/orchestrator/oracle_client.py already documents,
rather than pretending Toolbox can do something it can't.

ASSUMPTION FLAGGED FOR VERIFICATION: this connects McpToolset to the same
bare Toolbox base URL the toolbox-core SDK uses (ToolboxSyncClient's default
protocol is already native MCP -- see its `protocol` kwarg default). If your
deployed Toolbox version exposes the MCP endpoint at a different path (e.g.
`/mcp`), adjust `_MCP_URL` below. This couldn't be verified against a live
Toolbox server from the sandbox this was built in -- confirm it against your
actual running instance (agent.py's docstring has the one-line check).
"""

import json
import os
import time
from datetime import datetime, timezone

from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams
from toolbox_core import ToolboxSyncClient

REMEDIATION_TOOL_NAMES = [
    "kill_blocking_session",
    "kill_runaway_query",
    "flush_shared_pool",
    "increase_pga_target",
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
    """Toolbox SQL tools return a JSON-encoded string of result rows."""
    rows = json.loads(raw_result)
    if not rows:
        raise RuntimeError("query returned no rows")
    return rows[0]


def poll_telemetry(client: ToolboxSyncClient) -> dict:
    """Sense stage (Step 3): matches oracle_client.py's OracleClient.poll()
    return shape exactly (including the tick/ts stamps, which -- same as
    the original -- are added here in Python rather than by the query),
    so sense.py and predict.py (both copied unchanged) need no changes."""
    tool = client.load_tool("poll_telemetry")
    row = _first_row(tool())
    return {
        "tick": int(time.time()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "active_blocked_sessions": int(row["ACTIVE_BLOCKED_SESSIONS"]),
        "cpu_utilization_pct": int(row["CPU_UTILIZATION_PCT"]),
    }


def describe_state(client: ToolboxSyncClient) -> dict:
    """Act-stage pre-action snapshot (Risk 1): matches
    RealDbExecutor.describe_state()'s return shape."""
    tool = client.load_tool("describe_state")
    row = _first_row(tool())
    return {"active_blocked_sessions_snapshot": int(row["ACTIVE_BLOCKED_SESSIONS_SNAPSHOT"])}


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
    let alone raw SQL, as something it could call."""
    kwargs = dict(
        connection_params=StreamableHTTPConnectionParams(url=toolbox_url()),
        tool_filter=REMEDIATION_TOOL_NAMES,
    )
    if _auth_required():
        kwargs["header_provider"] = _mcp_header_provider
    return McpToolset(**kwargs)
