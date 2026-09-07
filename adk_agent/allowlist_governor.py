"""
allowlist_governor.py
Risk 7 mitigation: allowlist creep / IAM erosion.

Every entry in config.ALLOWLIST must have a corresponding, version-controlled
signoff record before act.py will treat it as usable. This module is the
single place that grants/records that signoff, and it is what test_safety.py
checks in CI -- a failing check here blocks the deploy (see cloudbuild.yaml
in the implementation guide, Step 9).

In a real deployment SIGNOFF_LEDGER would be a table (or an
`allowlist_signoffs.json` file reviewed via pull request) rather than an
in-memory dict; the interface -- grant_signoff() / has_valid_signoff() --
is what matters, and it's what act.py and test_safety.py depend on.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib


@dataclass(frozen=True)
class SignoffRecord:
    action_key: str
    approved_by: str    # security or DBA identity
    approved_at: str     # ISO timestamp
    statement_hash: str  # hash of the statement_template at approval time
    ticket: str           # change-ticket / PR reference


# In-memory ledger for the reference implementation.
SIGNOFF_LEDGER: dict = {}


def _hash_statement(statement_template: str) -> str:
    return hashlib.sha256(statement_template.encode()).hexdigest()[:12]


def grant_signoff(action_key: str, approved_by: str, ticket: str) -> SignoffRecord:
    """Record a signoff for an allowlist entry. Intended to be called by a
    human reviewer (e.g. from a small internal CLI/PR-merge hook), never
    from reason.py or act.py -- there is intentionally no automated path
    into this function from the agent's own execution path."""
    from config import ALLOWLIST

    if action_key not in ALLOWLIST:
        raise KeyError(f"Cannot sign off unknown action '{action_key}'")

    action = ALLOWLIST[action_key]
    record = SignoffRecord(
        action_key=action_key,
        approved_by=approved_by,
        approved_at=datetime.now(timezone.utc).isoformat(),
        statement_hash=_hash_statement(action.statement_template),
        ticket=ticket,
    )
    SIGNOFF_LEDGER[action_key] = record
    return record


def has_valid_signoff(action_key: str) -> bool:
    """An entry is valid only if it has a signoff AND the statement template
    hasn't changed since that signoff was granted -- this catches a silent
    edit to an already-approved statement, not just a missing approval."""
    from config import ALLOWLIST

    record = SIGNOFF_LEDGER.get(action_key)
    if record is None or action_key not in ALLOWLIST:
        return False
    current_hash = _hash_statement(ALLOWLIST[action_key].statement_template)
    return current_hash == record.statement_hash


def audit_unsigned_actions() -> list:
    """CI gate: any allowlist entry without a valid signoff fails the build.
    Called directly by test_safety.py."""
    from config import ALLOWLIST

    return [key for key in ALLOWLIST if not has_valid_signoff(key)]


# ---------------------------------------------------------------------------
# Reference-implementation bootstrap: grant signoffs for the shipped
# allowlist so the pipeline is runnable out of the box. In a real deployment,
# delete this block -- signoffs should only ever be granted by an actual
# reviewer, via a real change-management process.
# ---------------------------------------------------------------------------
def _bootstrap_reference_signoffs():
    for key in (
        "kill_blocking_session", "flush_shared_pool", "kill_runaway_query",
        "increase_pga_target", "restart_listener",
    ):
        grant_signoff(key, approved_by="demo-security-review", ticket="DEMO-0")


_bootstrap_reference_signoffs()
