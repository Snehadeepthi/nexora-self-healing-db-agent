"""
main.py
Flask entrypoint -- works unmodified as both the local dev server (pointed
at docker-compose.yml's Toolbox+Oracle) and the GCP Cloud Run image (see
../gcp_deploy/services/orchestrator, which is this same package pointed at
the deployed Toolbox Cloud Run service and a real Vertex AI project). Same
routes gcp_deploy's original main.py exposed, same shapes -- Cloud Scheduler
and README_DEPLOY.md's curl commands don't need to change.

Required environment variables:
  TOOLBOX_URL       Base URL of the MCP Toolbox server (default:
                     http://127.0.0.1:5000 -- docker-compose.yml's default).
  ORACLE_HOST/PORT/SERVICE/USER/PASSWORD
                     Only needed by the Toolbox server itself (tools.yaml
                     reads these) and by seed/simulate_blocking.py -- this
                     process never talks to Oracle directly.
  GEMINI_MODEL       Defaults to gemini-2.5-flash (see agent.py).
  To point the agent at Vertex AI instead of the Gemini Developer API,
  set the standard ADK/google-genai environment variables:
    GOOGLE_GENAI_USE_VERTEXAI=TRUE
    GOOGLE_CLOUD_PROJECT=<your project>
    GOOGLE_CLOUD_LOCATION=us-central1
  (Application Default Credentials handle auth -- `gcloud auth application-
  default login` locally, the Cloud Run service account in GCP. No manual
  vertexai.init() call needed, unlike the original llm_client.py -- ADK's
  google-genai client resolves these itself.)
"""

import logging
import os

from flask import Flask, jsonify

from pipeline import AdkOrchestrator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("self-healing-agent-adk")

app = Flask(__name__)

_orchestrator = None  # cold-start singleton, same pattern as gcp_deploy's original main.py


def state() -> AdkOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        logger.info("Cold start: connecting to Toolbox at %s", os.environ.get("TOOLBOX_URL", "http://127.0.0.1:5000"))
        _orchestrator = AdkOrchestrator()
        logger.info("Cold start complete")
    return _orchestrator


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/tick", methods=["POST"])
def tick():
    result = state().run_cycle()
    return jsonify(**result)


@app.route("/approve/<incident_id>", methods=["POST"])
def approve(incident_id):
    result = state().approve_and_resolve(incident_id)
    return jsonify(status=result["status"], detail=result.get("detail"))


@app.route("/compliance")
def compliance():
    return jsonify(state().audit_log.compliance_summary())


@app.route("/demo/trigger-tier3", methods=["POST"])
def demo_trigger_tier3():
    """Forces a Tier 3 action directly through the same before_tool gate a
    real incident would hit, skipping Sense/Predict/Reason -- exactly the
    shortcut the original demo.py/gcp main.py used, and for the same
    documented reason: skips waiting for real Oracle telemetry to
    organically produce a listener-outage-shaped incident. The gate
    downstream is identical either way -- it never auto-executes a Tier 3
    action, sends a real approval notification, and writes a real audit
    entry."""
    import uuid

    orch = state()
    incident_id = f"demo-tier3-{uuid.uuid4().hex[:8]}"

    class _Ctx:
        state = {"incident_id": incident_id, "query_text": "listener unreachable connection refused"}

    class _Tool:
        name = "restart_listener"

    result = orch.guardrails.before_tool(_Tool(), {"listener_name": "LISTENER"}, _Ctx())
    return jsonify(status=result["status"], detail=result.get("detail"), incident_id=incident_id)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
