"""
runbooks.py
Local/dev version of the Oracle 23ai VECTOR runbook store (Steps 5 and 7). Uses
a small hand-rolled bag-of-words cosine-distance index instead of a real
embedding model, so the reference pipeline has no external dependency.

Risk 4 mitigation: a newly learned runbook is NOT immediately eligible for
Reason-stage retrieval. It lands in `pending_review` and only moves into the
searchable `approved` store via `approve_pending_runbook()`, which stands in
for a human (on-call lead / DBA) review step.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field


def _tokenize(text: str):
    return re.findall(r"[a-z0-9_]+", text.lower())


def _vector(text: str) -> Counter:
    return Counter(_tokenize(text))


def _cosine_distance(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 1.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1 - (dot / (norm_a * norm_b))


@dataclass
class Runbook:
    title: str
    body: str
    resolution_action_key: str
    _vec: Counter = field(default_factory=Counter, init=False, repr=False)

    def __post_init__(self):
        self._vec = _vector(f"{self.title} {self.body}")


class RunbookStore:
    def __init__(self):
        self.approved = []
        self.pending_review = []
        self._seed_defaults()

    def _seed_defaults(self):
        self.approved.append(Runbook(
            title="Sustained blocked sessions",
            body="active_blocked_sessions sustained_high blocking lock contention",
            resolution_action_key="kill_blocking_session",
        ))
        self.approved.append(Runbook(
            title="Library cache contention",
            body="shared pool fragmentation high parse cpu_utilization_pct",
            resolution_action_key="flush_shared_pool",
        ))

    def nearest(self, query_text: str):
        """RAG retrieval -- only ever searches the APPROVED store (Risk 4)."""
        if not self.approved:
            return None, 1.0
        qv = _vector(query_text)
        best, best_dist = None, 1.0
        for rb in self.approved:
            d = _cosine_distance(qv, rb._vec)
            if d < best_dist:
                best, best_dist = rb, d
        return best, best_dist

    def nearest_distance_including_pending(self, query_text: str) -> float:
        """Used by learn.py's novelty check -- compares against everything
        already known, approved or not, so the same pattern doesn't get
        re-proposed as "novel" on every incident while it's awaiting review."""
        qv = _vector(query_text)
        best_dist = 1.0
        for rb in self.approved + self.pending_review:
            best_dist = min(best_dist, _cosine_distance(qv, rb._vec))
        return best_dist

    def submit_for_review(self, runbook: Runbook):
        self.pending_review.append(runbook)

    def approve_pending_runbook(self, title: str, reviewer: str):
        """Simulates a human (on-call lead / DBA) promoting a learned runbook
        into the searchable store."""
        match = next((rb for rb in self.pending_review if rb.title == title), None)
        if match is None:
            raise KeyError(f"No pending runbook titled '{title}'")
        self.pending_review.remove(match)
        self.approved.append(match)
        return match
