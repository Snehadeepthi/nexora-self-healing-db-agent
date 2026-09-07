"""
db_registry.py
Multi-database registry for the dashboard's database dropdown and the
per-engine real health check (Risk: dashboard "Healthy" previously only
reflected orchestrator process liveness, not actual DB/Toolbox
reachability -- see main.py's old /health, a static jsonify(status="ok")
with no DB call at all).

Each entry names the Toolbox tool-name prefix for that engine's tools.yaml
entries. Oracle's tools.yaml entries are unprefixed (poll_telemetry,
describe_database, ...) since it was the only engine when this project
started -- new engines get prefixed tool names (e.g. alloydb_poll_telemetry)
so nothing about Oracle's already-verified-working tools.yaml has to change
when a second engine is added. tool_name() below is the one place that
mapping happens.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DbEngine:
    id: str                  # used in ?db=<id> query params and the dashboard dropdown
    label: str                # display name in the dashboard dropdown
    tool_prefix: str          # "" for oracle (legacy unprefixed tools), "alloydb_" etc for new engines
    supports_statspack: bool  # Oracle-only panel; other engines hide it client-side
    health_tool: str          # which Toolbox tool name (before prefixing) backs the real health probe


DB_REGISTRY = {
    "oracle": DbEngine(
        id="oracle",
        label="Oracle (Self-Healing DB Agent)",
        tool_prefix="",
        supports_statspack=True,
        health_tool="poll_telemetry",
    ),
    # Add AlloyDB here once its prefixed tools.yaml entries exist (task #29):
    "alloydb": DbEngine(
        id="alloydb",
        label="AlloyDB (Postgres)",
        tool_prefix="alloydb_",
        supports_statspack=False,
        health_tool="poll_telemetry",
    ),
    "mysql": DbEngine(
        id="mysql",
        label="MySQL (Cloud SQL)",
        tool_prefix="mysql_",
        supports_statspack=False,
        health_tool="poll_telemetry",
    ),
}

DEFAULT_ENGINE = "oracle"


def get_engine(db_id: str) -> DbEngine:
    if db_id not in DB_REGISTRY:
        raise KeyError(f"unknown database id '{db_id}' -- valid: {sorted(DB_REGISTRY)}")
    return DB_REGISTRY[db_id]


def tool_name(engine: DbEngine, base_name: str) -> str:
    return f"{engine.tool_prefix}{base_name}"
