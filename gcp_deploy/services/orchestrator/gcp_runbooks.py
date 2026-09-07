"""
gcp_runbooks.py
Production Learn-stage runbook store (Step 5/7 of the implementation guide):
backs the exact same method surface as runbooks.RunbookStore -- nearest(),
nearest_distance_including_pending(), submit_for_review(),
approve_pending_runbook() -- with the real BigQuery db_ops.runbooks table
instead of an in-memory list, so reason.py and learn.py -- which only ever
call these methods on whatever store object they're handed -- need no
changes at all.

The bag-of-words cosine-distance retrieval itself is reused UNCHANGED from
runbooks.py (imported directly below); only the storage backend changes.
Swap this for Oracle 23ai VECTOR + a real embedding model per the reference
README when ready -- nearest() / submit_for_review() /
approve_pending_runbook() are the only methods any caller depends on, so
that later swap is contained entirely to this one file too.

Known limitation: BigQuery's streaming insert buffer can briefly block an
UPDATE on a just-inserted row (usually clears within minutes). If
approve_pending_runbook() fails with a "streaming buffer" error immediately
after a runbook was submitted, wait a minute and retry.
"""
import os
from datetime import datetime, timezone

from google.cloud import bigquery

from runbooks import Runbook, _cosine_distance, _vector


class BigQueryRunbookStore:
    def __init__(self, project_id=None, dataset="db_ops", table="runbooks"):
        self.project_id = project_id or os.environ["GCP_PROJECT"]
        self.dataset = dataset
        self.table = table
        self.client = bigquery.Client(project=self.project_id)
        self._table_ref = f"{self.project_id}.{self.dataset}.{self.table}"
        self.approved = []
        self.pending_review = []
        self._load()

    def _load(self):
        query = f"SELECT title, body, resolution_action_key, status FROM `{self._table_ref}`"
        self.approved, self.pending_review = [], []
        for row in self.client.query(query).result():
            rb = Runbook(title=row["title"], body=row["body"], resolution_action_key=row["resolution_action_key"])
            (self.approved if row["status"] == "approved" else self.pending_review).append(rb)
        if not self.approved and not self.pending_review:
            self._seed_defaults()

    def _seed_defaults(self):
        """First-ever boot: seed the same two starter runbooks the local
        reference implementation ships with, so Reason has something to
        match against from tick one."""
        defaults = [
            ("Sustained blocked sessions",
             "active_blocked_sessions sustained_high blocking lock contention",
             "kill_blocking_session"),
            ("Library cache contention",
             "shared pool fragmentation high parse cpu_utilization_pct",
             "flush_shared_pool"),
        ]
        for title, body, action_key in defaults:
            self._insert_row(title, body, action_key, "approved", reviewed_by="reference-seed")
            self.approved.append(Runbook(title=title, body=body, resolution_action_key=action_key))

    def _insert_row(self, title, body, action_key, status, reviewed_by=None):
        row = {
            "title": title, "body": body, "resolution_action_key": action_key,
            "status": status, "created_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_by": reviewed_by,
        }
        errors = self.client.insert_rows_json(self._table_ref, [row])
        if errors:
            raise RuntimeError(f"BigQuery insert failed: {errors}")

    def nearest(self, query_text: str):
        """RAG retrieval -- only ever searches the approved store (Risk 4),
        identical logic to the reference implementation."""
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
        qv = _vector(query_text)
        best_dist = 1.0
        for rb in self.approved + self.pending_review:
            best_dist = min(best_dist, _cosine_distance(qv, rb._vec))
        return best_dist

    def submit_for_review(self, runbook: Runbook):
        self._insert_row(runbook.title, runbook.body, runbook.resolution_action_key, "pending_review")
        self.pending_review.append(runbook)

    def approve_pending_runbook(self, title: str, reviewer: str):
        match = next((rb for rb in self.pending_review if rb.title == title), None)
        if match is None:
            raise KeyError(f"No pending runbook titled '{title}'")
        query = f"""
            UPDATE `{self._table_ref}`
            SET status = 'approved', reviewed_by = @reviewer
            WHERE title = @title AND status = 'pending_review'
        """
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("reviewer", "STRING", reviewer),
            bigquery.ScalarQueryParameter("title", "STRING", title),
        ])
        self.client.query(query, job_config=job_config).result()
        self.pending_review.remove(match)
        self.approved.append(match)
        return match
