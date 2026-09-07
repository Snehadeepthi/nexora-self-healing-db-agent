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
}


# Maintenance windows (UTC). Format: (weekday 0=Mon..6=Sun, start_time, end_time).
# Readings taken inside one of these are never counted as anomaly candidates.
MAINTENANCE_WINDOWS = [
    (6, time(1, 0), time(4, 0)),      # Sunday 01:00-04:00 UTC, weekly batch job
    (0, time(23, 0), time(23, 59)),   # Monday month-end close spike guard
]

# Risk 2: a single anomalous reading never triggers an action on its own.
CONSECUTIVE_ANOMALY_THRESHOLD = 2

# Risk 4: cosine distance beyond this means "genuinely novel" runbook.
NOVELTY_THRESHOLD = 0.15

# Risk 6: cost guardrails for the Reason stage's Vertex AI calls.
MAX_GEMINI_CALLS_PER_WINDOW = 20      # per rolling window
GEMINI_CALL_WINDOW_SECONDS = 600      # 10 minutes
MONTHLY_BUDGET_USD = 2500

# Risk 5: circuit breaker tuning for each external dependency.
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
CIRCUIT_BREAKER_RESET_SECONDS = 120

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
