"""
pipeline.py
Wires Sense -> Predict -> Reason/Act -> Learn into a single closed loop --
the ADK-native equivalent of orchestrator.py. Sense and Predict stay exactly
as deterministic as they were (predict.py is copied unchanged; sense.py is
copied unchanged and driven by a tiny shim so it never has to know its
'oracle_client' is now backed by MCP Toolbox instead of oracledb/a
simulator). Reason+Act is now one ADK agent turn instead of two separate
manual calls -- see agent.py's docstring for why that's a genuine upgrade,
not just a relabeling. Learn stays exactly as deterministic as before too.

audit_log/runbook_store/notifier are constructor-injected (defaulting to
this package's local in-memory stand-ins) rather than hardcoded, the same
dependency-injection shape act.py/reason.py always used -- this is what
lets this exact file be copied byte-identical into
gcp_deploy/services/orchestrator, where main.py instead injects
gcp_audit.BigQueryAuditLog / gcp_runbooks.BigQueryRunbookStore /
gcp_notifications.SlackNotifier. Nothing in this file needs to know or care
which one it's holding, same as every other guardrail file in this project.
"""

import asyncio

import config
import db_tools
import learn
import sense
from google.adk import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import build_agent
from guardrail_callbacks import Guardrails
from predict import AnomalyDetector

APP_NAME = "self-healing-db-agent"
USER_ID = "orchestrator"


class _ReadingSource:
    """Satisfies sense.poll()'s `oracle_client.poll()` interface -- the same
    shape OracleSimulator and oracle_client.OracleClient satisfy -- while
    actually reading through MCP Toolbox. sense.py never needs to know or
    care which backend is behind it."""

    def __init__(self, db_client):
        self._db_client = db_client

    def poll(self):
        return db_tools.poll_telemetry(self._db_client)


class AdkOrchestrator:
    def __init__(self, audit_log=None, runbook_store=None, notifier=None):
        if audit_log is None:
            from audit import AuditLog
            audit_log = AuditLog()
        if runbook_store is None:
            from runbooks import RunbookStore
            runbook_store = RunbookStore()
        if notifier is None:
            from notifications import EmailNotifier
            notifier = EmailNotifier()

        self.audit_log = audit_log
        self.runbook_store = runbook_store
        self.notifier = notifier
        self.detector = AnomalyDetector(self.audit_log)
        self.guardrails = Guardrails(self.audit_log, notifier=self.notifier)
        self.agent = build_agent(self.guardrails)
        self.session_service = InMemorySessionService()
        self.runner = Runner(
            agent=self.agent, app_name=APP_NAME, session_service=self.session_service
        )
        self._db_client = db_tools.get_sync_client()
        self._reading_source = _ReadingSource(self._db_client)

    # -----------------------------------------------------------------
    # One Sense -> Predict -> Reason/Act -> Learn tick. Returns the same
    # small trace-dict shape orchestrator.py's run_cycle() does, for
    # demo.py/main.py parity.
    # -----------------------------------------------------------------
    def run_cycle(self) -> dict:
        try:
            reading = sense.poll(self._reading_source)
        except Exception as e:
            self.audit_log.log("breaker_tripped", dependency="oracle_db_sense", detail=str(e))
            return {"stage": "sense", "outcome": "oracle_unreachable", "detail": str(e)}

        event = self.detector.score(reading)
        if event is None:
            return {"stage": "predict", "outcome": "no_confirmed_anomaly", "reading": reading}

        runbook, distance = self.runbook_store.nearest(event["query_text"])
        outcome = asyncio.run(self._reason_and_act(event, runbook))

        if outcome["status"] is None:
            return {"stage": "reason", "outcome": "no_action_proposed", "event": event}

        if outcome["status"] == "EXECUTED":
            # Reference-implementation assumption carried over verbatim from
            # orchestrator.py: a real deployment re-polls telemetry before
            # calling learn() to confirm the fix actually worked.
            learn.learn_from_incident(
                event, outcome["action_payload"], True,
                self.runbook_store, self.audit_log, notifier=self.notifier,
            )

        return {
            "stage": "act", "outcome": outcome["status"], "detail": outcome.get("detail"),
            "event": event, "action_payload": outcome.get("action_payload"),
        }

    async def _reason_and_act(self, event: dict, runbook) -> dict:
        incident_id = event["incident_id"]
        session = await self.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=incident_id,
            state={"incident_id": incident_id, "query_text": event.get("query_text", "")},
        )
        message = types.Content(role="user", parts=[types.Part(text=_incident_prompt(event, runbook))])

        events = []
        async for e in self.runner.run_async(
            user_id=USER_ID, session_id=session.id, new_message=message
        ):
            events.append(e)
        return _extract_outcome(events, incident_id)

    # -----------------------------------------------------------------
    # Tier 3 human approval -- mirrors act.py's GuardedExecutionEngine.
    # approve() calling self._run() directly, never re-asking the LLM.
    # -----------------------------------------------------------------
    def approve_and_resolve(self, incident_id: str) -> dict:
        pending = self.guardrails.pending_approvals.pop(incident_id, None)
        if pending is None:
            return {"status": "REJECTED", "detail": "no pending approval for this incident"}

        action_key, params, snapshot = pending["action_key"], pending["params"], pending["snapshot"]
        try:
            if action_key == "restart_listener":
                db_tools.restart_listener(**params)  # always raises -- see its docstring
            else:
                db_tools.run_remediation_directly(self._db_client, action_key, params)
        except Exception as e:
            self.audit_log.log("action_failed", incident_id=incident_id, detail=str(e), snapshot=snapshot)
            return {"status": "REJECTED", "detail": f"execution failed: {e}"}

        statement = config.ALLOWLIST[action_key].statement_template.format(**params)
        self.audit_log.log("action_executed", incident_id=incident_id, action_key=action_key,
                            statement=statement, snapshot=snapshot)

        action_payload = {
            "action_key": action_key, "params": params, "incident_id": incident_id,
            "query_text": pending.get("query_text", ""),
        }
        learn.learn_from_incident(
            action_payload, action_payload, True,
            self.runbook_store, self.audit_log, notifier=self.notifier,
        )
        return {"status": "EXECUTED", "detail": statement}


def _incident_prompt(event: dict, runbook) -> str:
    return (
        "Incident details:\n"
        f"  metric: {event.get('metric')}\n"
        f"  status: {event.get('status')}\n"
        f"  value: {event.get('value')}\n"
        f"  query_text: {event.get('query_text')}\n"
        f"  nearest known runbook: {runbook.title if runbook else 'none'}"
        f" -- {runbook.body if runbook else ''}\n"
    )


def _extract_outcome(events, incident_id: str) -> dict:
    """Walks the event stream for the tool call the model made (if any) and
    the function_response that came back. Short-circuited results
    (REJECTED/PENDING_APPROVAL/BREAKER_OPEN) come from guardrail_callbacks.py
    itself and always carry an explicit 'status' key; a genuinely-executed
    real MCP tool call's response is Toolbox's own raw result and never
    happens to have that key -- so its absence (with a tool call present)
    is what "EXECUTED" is inferred from."""
    action_key, args, status, detail = None, None, None, None
    for e in events:
        for fc in e.get_function_calls():
            action_key, args = fc.name, dict(fc.args or {})
        for fr in e.get_function_responses():
            resp = fr.response or {}
            status = resp.get("status", status)
            detail = resp.get("detail", detail)

    if action_key is None:
        return {"status": None}

    return {
        "status": status or "EXECUTED",
        "detail": detail,
        "action_payload": {"action_key": action_key, "params": args, "incident_id": incident_id},
    }
