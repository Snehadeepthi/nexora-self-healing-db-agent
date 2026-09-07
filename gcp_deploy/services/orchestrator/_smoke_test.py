"""
_smoke_test.py
Local, mocked-GCP validation for the ADK + MCP Toolbox rebuild -- not shipped
to Cloud Run (see .dockerignore), not part of the CI safety gate
(test_safety.py is). This exists to prove gcp_audit.BigQueryAuditLog,
gcp_runbooks.BigQueryRunbookStore, and gcp_notifications.SlackNotifier are
genuinely drop-in replacements for the local audit.AuditLog /
runbooks.RunbookStore / notifications.EmailNotifier stand-ins
guardrail_callbacks.Guardrails and pipeline.AdkOrchestrator were built
against -- by running (a subset of) the exact same guardrail assertions
test_safety.py runs, but with the real-shaped GCP classes plugged in
instead, against mocked bigquery/secretmanager/requests clients so this
runs with no live GCP project needed.

Run: python _smoke_test.py
"""
import sys
import types as pytypes
from unittest import mock


def _install_fake_gcp_modules():
    """Stub out google.cloud.bigquery / google.cloud.secretmanager /
    requests before gcp_audit.py / gcp_runbooks.py / gcp_secrets.py /
    gcp_notifications.py import them, so none of this needs real
    credentials or network access."""

    class _FakeQueryResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def result(self):
            return self._rows

    class _FakeBQClient:
        def __init__(self, *a, **k):
            self.inserted = []

        def insert_rows_json(self, table_ref, rows):
            self.inserted.extend(rows)
            return []  # empty error list == success, matches real client's contract

        def query(self, sql, job_config=None):
            return _FakeQueryResult([])  # empty table -- exercises the "seed defaults" path

    bigquery_mod = pytypes.ModuleType("google.cloud.bigquery")
    bigquery_mod.Client = _FakeBQClient
    bigquery_mod.QueryJobConfig = lambda **k: k
    bigquery_mod.ScalarQueryParameter = lambda *a, **k: (a, k)

    class _FakeSecretResponse:
        payload = mock.Mock(data=b"fake-slack-webhook-url-value")

    class _FakeSecretClient:
        def access_secret_version(self, name):
            return _FakeSecretResponse()

    secretmanager_mod = pytypes.ModuleType("google.cloud.secretmanager")
    secretmanager_mod.SecretManagerServiceClient = _FakeSecretClient

    # google.cloud is a namespace package -- it only exists once some real
    # google-cloud-* package is installed. Register a stub so
    # `from google.cloud import bigquery` resolves without one.
    if "google.cloud" not in sys.modules:
        cloud_mod = pytypes.ModuleType("google.cloud")
        cloud_mod.__path__ = []  # mark as a package
        sys.modules["google.cloud"] = cloud_mod
        import google
        google.cloud = cloud_mod

    sys.modules["google.cloud.bigquery"] = bigquery_mod
    sys.modules["google.cloud.secretmanager"] = secretmanager_mod
    google_cloud = sys.modules["google.cloud"]
    google_cloud.bigquery = bigquery_mod
    google_cloud.secretmanager = secretmanager_mod


def main():
    _install_fake_gcp_modules()

    import requests
    with mock.patch.object(requests, "post") as fake_post:
        fake_post.return_value = mock.Mock(raise_for_status=lambda: None)

        from gcp_audit import BigQueryAuditLog
        from gcp_notifications import SlackNotifier
        from gcp_runbooks import BigQueryRunbookStore
        import gcp_secrets

        audit_log = BigQueryAuditLog(project_id="fake-project")
        runbook_store = BigQueryRunbookStore(project_id="fake-project")
        notifier = SlackNotifier(webhook_url="https://hooks.slack.test/fake")

        assert gcp_secrets.get_secret("fake-secret", "fake-project") == "fake-slack-webhook-url-value"
        print("[ok] gcp_secrets.get_secret works against a mocked Secret Manager client")

        from guardrail_callbacks import Guardrails
        import config

        g = Guardrails(audit_log, notifier=notifier)

        class _FakeTool:
            def __init__(self, name):
                self.name = name

        class _FakeCtx:
            def __init__(self, incident_id):
                self.state = {"incident_id": incident_id, "query_text": ""}

        # Tier 1, valid params -> passes through
        r = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 101, "serial": 202}, _FakeCtx("s1"))
        assert r is None
        print("[ok] Tier 1 action passes before_tool against BigQueryAuditLog")

        # Tier 3 -> pending approval, real Slack notification attempted (mocked)
        r = g.before_tool(_FakeTool("restart_listener"), {"listener_name": "LISTENER"}, _FakeCtx("s2"))
        assert r["status"] == "PENDING_APPROVAL"
        assert len(notifier.outbox) == 1
        assert "Approval needed" in notifier.outbox[0].subject
        print("[ok] Tier 3 action pends approval and sends a (mocked) Slack notification")

        # Hallucination firewall still works with the GCP audit log behind it
        r = g.before_tool(_FakeTool("kill_blocking_session"), {"sid": 101}, _FakeCtx("s3"))
        assert r["status"] == "REJECTED"
        print("[ok] hallucination firewall (missing param) rejects, logs to BigQueryAuditLog")

        # RunbookStore seeded with defaults on first (empty-table) load
        assert len(runbook_store.approved) == 2
        rb, dist = runbook_store.nearest("active_blocked_sessions sustained_high blocking lock contention")
        assert rb is not None and rb.resolution_action_key == "kill_blocking_session"
        print("[ok] BigQueryRunbookStore seeds defaults and nearest() retrieval works")

        # main.py's Flask app + routes construct without a live GCP project
        # (state() itself is NOT called here -- that needs a real Toolbox
        # connection, which is exactly what this offline smoke test can't
        # provide; see agent.py/pipeline.py's own tests for guardrail logic).
        import main as gcp_main
        routes = sorted(r.rule for r in gcp_main.app.url_map.iter_rules())
        expected = {"/health", "/tick", "/approve/<incident_id>", "/compliance", "/demo/trigger-tier3"}
        assert expected <= set(routes), routes
        print("[ok] main.py Flask app constructs with all expected routes:", sorted(expected))

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
