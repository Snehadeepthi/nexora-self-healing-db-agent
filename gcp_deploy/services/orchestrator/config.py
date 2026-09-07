"""
config.py
Central policy: the SQL/action allowlist, tiering, maintenance windows,
anomaly-confirmation thresholds, and cost guardrails.

Each constant here maps to a specific risk from the risk register:
  - ALLOWLIST + allowlist_governor.SIGNOFF_LEDGER -> Risk 7 (Allowlist creep / IAM erosion)
  - MAINTENANCE_WINDOWS                            -> Risk 2 (False-positive anomalies)
  - CONSECUTIVE_ANOMALY_THRESHOLD                  -> Risk 2 (False-positive anomalies)
  - NOVELTY_THRESHOLD                              -> Risk 4 (Uncontrolled runbook drift)
  - MAX_GEMINI_CALLS_PER_WINDOW / MONTHLY_BUDGET    -> Risk 6 (Cost overrun)
  - CIRCUIT_BREAKER_*                               -> Risk 5 (Multi-vendor coupling)
"""

from dataclasses import dataclass
from datetime import time
from enum import IntEnum


class Tier(IntEnum):
    TIER_1 = 1  # fully automatic, low blast radius (e.g. kill a single blocking session)
    TIER_2 = 2  # automatic but logged loudly + paged (e.g. flush the shared pool)
    TIER_3 = 3  # requires human approval before executing (e.g. restart, instance-wide change)


@dataclass(frozen=True)
class AllowlistedAction:
    action_key: str
    description: str
    tier: Tier
    statement_template: str      # parameterized statement, never freeform SQL from the model
    required_privileges: tuple   # e.g. ("ALTER SYSTEM",) -- never "DBA"
    param_schema: dict           # {param_name: python_type}; anything else is rejected
    engine: str = "oracle"       # db_registry engine id this action runs against


# ---------------------------------------------------------------------------
# Risk 7 mitigation: every entry here needs an explicit, versioned signoff
# record (see allowlist_governor.py) before act.py will treat it as usable.
# test_safety.py fails the CI build if any entry is missing one.
# ---------------------------------------------------------------------------
ALLOWLIST = {
    "kill_blocking_session": AllowlistedAction(
        action_key="kill_blocking_session",
        description="Terminate a single session holding a blocking lock.",
        tier=Tier.TIER_1,
        statement_template="ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE",
        required_privileges=("ALTER SYSTEM",),
        param_schema={"sid": int, "serial": int},
    ),
    "flush_shared_pool": AllowlistedAction(
        action_key="flush_shared_pool",
        description="Flush the shared pool to relieve library cache contention.",
        tier=Tier.TIER_2,
        statement_template="ALTER SYSTEM FLUSH SHARED_POOL",
        required_privileges=("ALTER SYSTEM",),
        param_schema={},
    ),
    "kill_runaway_query": AllowlistedAction(
        action_key="kill_runaway_query",
        description="Terminate a single runaway/long-running query session.",
        tier=Tier.TIER_1,
        statement_template="ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE",
        required_privileges=("ALTER SYSTEM",),
        param_schema={"sid": int, "serial": int},
    ),
    "increase_pga_target": AllowlistedAction(
        action_key="increase_pga_target",
        description="Raise PGA_AGGREGATE_TARGET by one step to relieve memory pressure.",
        tier=Tier.TIER_3,   # instance-wide -> requires approval
        statement_template="ALTER SYSTEM SET PGA_AGGREGATE_TARGET = {target_mb}M SCOPE=BOTH",
        required_privileges=("ALTER SYSTEM",),
        param_schema={"target_mb": int},
    ),
    "restart_listener": AllowlistedAction(
        action_key="restart_listener",
        description="Restart the Oracle listener process.",
        tier=Tier.TIER_3,   # service-affecting -> requires approval
        statement_template="!lsnrctl restart {listener_name}",
        required_privileges=("ALTER SYSTEM",),  # scoped OS-level exec, never DBA
        param_schema={"listener_name": str},
    ),
    # -----------------------------------------------------------------
    # AlloyDB (Postgres) actions -- Postgres-idiomatic, not literal Oracle
    # mirrors. No ALTER SYSTEM: managed-instance support for it isn't
    # something to assert without live verification, so every action here
    # uses only pg_terminate_backend, which is guaranteed available.
    # -----------------------------------------------------------------
    "alloydb_kill_blocking_session": AllowlistedAction(
        action_key="alloydb_kill_blocking_session",
        description="Terminate a single Postgres backend holding a blocking lock.",
        tier=Tier.TIER_1,
        statement_template="SELECT pg_terminate_backend({pid})",
        required_privileges=("pg_signal_backend",),
        param_schema={"pid": int},
        engine="alloydb",
    ),
    "alloydb_kill_runaway_query": AllowlistedAction(
        action_key="alloydb_kill_runaway_query",
        description="Terminate a single runaway/long-running query's backend.",
        tier=Tier.TIER_1,
        statement_template="SELECT pg_terminate_backend({pid})",
        required_privileges=("pg_signal_backend",),
        param_schema={"pid": int},
        engine="alloydb",
    ),
    "alloydb_terminate_idle_in_transaction": AllowlistedAction(
        action_key="alloydb_terminate_idle_in_transaction",
        description="Batch-terminate backends stuck idle-in-transaction past a threshold.",
        tier=Tier.TIER_2,   # can affect multiple sessions -> broader blast radius than Tier 1
        statement_template=(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE state = 'idle in transaction' "
            "AND state_change < now() - interval '{idle_seconds} seconds'"
        ),
        required_privileges=("pg_signal_backend",),
        param_schema={"idle_seconds": int},
        engine="alloydb",
    ),
    "alloydb_set_statement_timeout": AllowlistedAction(
        action_key="alloydb_set_statement_timeout",
        description=(
            "Set a database-level statement_timeout to prevent an immediate "
            "runaway-query recurrence right after alloydb_kill_runaway_query "
            "terminates the current offender. Deterministically triggered by "
            "pipeline.py -- never LLM-selected. Genuinely reversible: the "
            "exact prior value is captured before this runs (see "
            "db_tools.get_statement_timeout) and can be replayed verbatim to "
            "undo it (see main.py's /rollback endpoint)."
        ),
        tier=Tier.TIER_2,
        statement_template="ALTER DATABASE postgres SET statement_timeout = '{statement_timeout_value}'",
        required_privileges=("ALTER DATABASE",),
        param_schema={"statement_timeout_value": str},
        engine="alloydb",
    ),
    "alloydb_reset_all_connections": AllowlistedAction(
        action_key="alloydb_reset_all_connections",
        description="Terminate every other active session on this database (last-resort reset).",
        tier=Tier.TIER_3,   # instance-wide -> requires approval
        statement_template=(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE pid <> pg_backend_pid() AND datname = current_database()"
        ),
        required_privileges=("pg_signal_backend",),
        param_schema={},
        engine="alloydb",
    ),
    # -----------------------------------------------------------------
    # MySQL (Cloud SQL) actions. Tier 1 is a plain KILL <processlist_id>
    # (same templateParameters reasoning as Oracle's ALTER SYSTEM -- KILL
    # doesn't accept a normal bind variable either). Tier 2/3 terminate
    # MORE THAN ONE connection per action, and MySQL's KILL has no
    # subquery/WHERE-clause form the way pg_terminate_backend(pid) FROM
    # ... does -- so those two call a stored procedure instead (see
    # gcp_deploy/tools_db/mysql_procedures.sql).
    # -----------------------------------------------------------------
    "mysql_kill_blocking_session": AllowlistedAction(
        action_key="mysql_kill_blocking_session",
        description="Terminate a single MySQL connection holding a blocking InnoDB row lock.",
        tier=Tier.TIER_1,
        statement_template="KILL {processlist_id}",
        required_privileges=("PROCESS", "CONNECTION_ADMIN"),
        param_schema={"processlist_id": int},
        engine="mysql",
    ),
    "mysql_kill_runaway_query": AllowlistedAction(
        action_key="mysql_kill_runaway_query",
        description="Terminate a single runaway/long-running query's connection.",
        tier=Tier.TIER_1,
        statement_template="KILL {processlist_id}",
        required_privileges=("PROCESS", "CONNECTION_ADMIN"),
        param_schema={"processlist_id": int},
        engine="mysql",
    ),
    "mysql_terminate_idle_in_transaction": AllowlistedAction(
        action_key="mysql_terminate_idle_in_transaction",
        description="Batch-terminate connections stuck idle-in-transaction past a threshold.",
        tier=Tier.TIER_2,   # can affect multiple connections -> broader blast radius than Tier 1
        statement_template="CALL sp_mysql_terminate_idle_in_transaction({idle_seconds})",
        required_privileges=("PROCESS", "CONNECTION_ADMIN"),
        param_schema={"idle_seconds": int},
        engine="mysql",
    ),
    "mysql_reset_all_connections": AllowlistedAction(
        action_key="mysql_reset_all_connections",
        description="Terminate every other active connection on this instance (last-resort reset).",
        tier=Tier.TIER_3,   # instance-wide -> requires approval
        statement_template="CALL sp_mysql_reset_all_connections()",
        required_privileges=("PROCESS", "CONNECTION_ADMIN"),
        param_schema={},
        engine="mysql",
    ),
}


# Maintenance windows (UTC). Format: (weekday 0=Mon..6=Sun, start_time, end_time).
# Readings taken inside one of these are never counted as anomaly candidates.
MAINTENANCE_WINDOWS = [
    (6, time(1, 0), time(4, 0)),      # Sunday 01:00-04:00 UTC, weekly batch job
    (0, time(23, 0), time(23, 59)),   # Monday month-end close spike guard
]

# Risk 2: a single anomalous reading never triggers an action on its own.
CONSECUTIVE_ANOMALY_THRESHOLD = 2

# Risk 2 (AlloyDB signals): same false-positive mitigation, one threshold
# per new AlloyDB-only signal predict.py tracks.
RUNAWAY_QUERY_SECONDS_THRESHOLD = 60          # a query running this long = candidate
IDLE_IN_TRANSACTION_COUNT_THRESHOLD = 3       # this many stuck backends = candidate
CONNECTION_PCT_THRESHOLD = 0.8

# Risk 4: cosine distance beyond this means "genuinely novel" runbook.
NOVELTY_THRESHOLD = 0.15

# Risk 6: cost guardrails for the Reason stage's Vertex AI calls.
MAX_GEMINI_CALLS_PER_WINDOW = 20      # per rolling window
GEMINI_CALL_WINDOW_SECONDS = 600      # 10 minutes
MONTHLY_BUDGET_USD = 2500

# Risk 5: circuit breaker tuning for each external dependency.
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
CIRCUIT_BREAKER_RESET_SECONDS = 120

# Tier 3 approval TTL: a pending_approvals entry with no human response
# within this window is auto-expired (see guardrail_callbacks.py's
# expire_stale_approvals) rather than left to accumulate indefinitely.
# During something like a lock cascade, an approval no one ever sees means
# nothing acts while the underlying problem keeps getting worse -- this
# bounds how long that silence can last. 900s = 15 minutes: short enough a
# cascade doesn't fester, long enough a human has a real shot at the Slack
# ping.
TIER3_APPROVAL_TTL_SECONDS = 900

# Tier 3 suppression auto-clear: normally clear_suppression() is an
# explicit operator action (see guardrail_callbacks.py's
# expire_stale_approvals docstring for why that's deliberate) -- but
# requiring a human to notice and reset it after every transient trip
# adds toil for the common case where the underlying condition really
# did resolve on its own. If an engine's own Sense/Predict stage reports
# a clean reading (no confirmed anomaly) for this many CONSECUTIVE ticks,
# every suppression still armed for that engine is auto-cleared (see
# Guardrails.record_tick_health()) and logged as
# action_suppression_auto_cleared -- a distinct event from the manual
# suppression_cleared, so the audit trail always shows which path acted.
# 3 ticks x the 60s Cloud Scheduler cadence = ~3 minutes of sustained
# health before auto-clearing -- long enough that a single lucky quiet
# reading during a still-ongoing storm can't un-suppress it prematurely.
SUPPRESSION_AUTO_CLEAR_HEALTHY_TICKS = 3

# Design rule for future Oracle actions: every Oracle action currently
# in ALLOWLIST is ALTER SYSTEM / session-level tuning (kill a session,
# flush the shared pool, adjust PGA target, restart the listener) -- none
# perform schema-level DDL, which is why ORA-65066 (a common-user-schema
# DDL error under a multitenant CDB) doesn't apply today. If a future
# action ever needs to run DDL against a CDB, its statement_template must
# explicitly include CONTAINER=ALL, or it risks ORA-65066 and a doomed
# retry loop instead of a clean failure.

# Email notification routing (see notifications.py). Two distinct audiences:
# Tier 3 approval requests need a human who can act *right now*; status
# reports are informational and can fan out to a wider team distribution list.
APPROVAL_NOTIFY_EMAIL = "sre-oncall@yourcompany.com"
STATUS_REPORT_EMAIL = "sre-team@yourcompany.com"


def get_action(action_key: str) -> AllowlistedAction:
    """Look up an allowlisted action. Raises if unknown OR unsigned (Risk 7).
    This is the single choke point act.py uses -- there is no other path to
    an AllowlistedAction object, so a revoked/missing signoff always blocks
    execution regardless of what the model proposed."""
    from allowlist_governor import has_valid_signoff  # local import avoids a cycle

    if action_key not in ALLOWLIST:
        raise KeyError(f"'{action_key}' is not an allowlisted action")
    if not has_valid_signoff(action_key):
        raise PermissionError(
            f"'{action_key}' exists in ALLOWLIST but has no recorded security/DBA "
            f"signoff -- refusing to treat it as executable (Risk 7 guardrail)."
        )
    return ALLOWLIST[action_key]
