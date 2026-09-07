"""
gcp_notifications.py
Production notification channel (Step 6, alongside the implementation
guide's Slack webhook): implements the same method surface as
notifications.EmailNotifier -- send_approval_request() and
send_status_report(), each returning an object with a .to attribute -- so
act.py and learn.py, which only ever call notifier.send_approval_request(...)
/ notifier.send_status_report(...) and read the return value's .to, work
completely unmodified. Posts to a Slack incoming webhook instead of sending
real email; swap _send()'s body for smtplib/SendGrid to add an email channel
alongside this one, exactly as notifications.py's own docstring describes.
"""
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


@dataclass
class SentNotification:
    to: str
    subject: str
    body: str
    sent_at: str


class SlackNotifier:
    def __init__(self, webhook_url: str, channel_label: str = "#sre-agent"):
        self.webhook_url = webhook_url
        self.channel_label = channel_label
        self.outbox = []  # kept for parity with EmailNotifier.outbox; Slack itself is the durable record

    def _send(self, subject: str, body: str) -> SentNotification:
        payload = {"text": f"*{subject}*\n```{body}```"}
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            # A notification failure must never block the guardrail pipeline
            # itself from recording what happened -- log loudly, keep going.
            logger.error("Slack notification failed: %s", e)
        note = SentNotification(
            to=self.channel_label, subject=subject, body=body,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
        self.outbox.append(note)
        return note

    # ---- Risk 1 / Step 6: Tier 3 approval notification ----
    def send_approval_request(self, action_payload: dict, action) -> SentNotification:
        subject = f"[SRE Agent] Approval needed: {action.action_key} (Tier {int(action.tier)})"
        body = (
            f"An automated diagnosis proposes a Tier {int(action.tier)} action that "
            f"requires human approval before it will run.\n\n"
            f"Incident:     {action_payload.get('incident_id')}\n"
            f"Action:       {action.action_key} -- {action.description}\n"
            f"Parameters:   {action_payload.get('params')}\n"
            f"Statement:    {action.statement_template}\n\n"
            f"Approve by POSTing to /approve/{action_payload.get('incident_id')} "
            f"on the orchestrator Cloud Run service."
        )
        return self._send(subject, body)


    # ---- Tier 3 approval TTL expiry notification ----
    def send_approval_timeout(self, action_payload: dict, detail: str) -> SentNotification:
        subject = f"[SRE Agent] Tier 3 approval EXPIRED: {action_payload.get('action_key')}"
        body = (
            f"A Tier 3 action was proposed and awaiting approval, but no one approved it "
            f"in time -- it has been automatically aborted WITHOUT executing.\n\n"
            f"Incident:     {action_payload.get('incident_id')}\n"
            f"Action:       {action_payload.get('action_key')}\n"
            f"Parameters:   {action_payload.get('params')}\n"
            f"Detail:       {detail}\n\n"
            f"The underlying condition that triggered this proposal may still be "
            f"unresolved -- check the dashboard for this incident."
        )
        return self._send(subject, body)

    # ---- Risk 3 / Risk 8: post-resolution status report ----
    def send_status_report(self, incident_id: str, action_payload: dict, audit_log, healthy: bool) -> SentNotification:
        mttd = audit_log.mttd_seconds(incident_id)
        mttr = audit_log.mttr_seconds(incident_id)
        status_word = "RESOLVED" if healthy else "FIX APPLIED BUT UNHEALTHY"
        subject = f"[SRE Agent] {status_word}: incident {incident_id}"
        body = (
            f"Incident {incident_id} has been processed by the self-healing pipeline.\n\n"
            f"Status:          {status_word}\n"
            f"Action taken:    {action_payload.get('action_key')}\n"
            f"Parameters:      {action_payload.get('params')}\n"
            f"MTTD:            {mttd if mttd is not None else 'n/a'} seconds\n"
            f"MTTR:            {mttr if mttr is not None else 'n/a'} seconds\n\n"
            + ("Telemetry confirms the anomaly has cleared. No further action needed."
               if healthy else
               "Telemetry has NOT returned to baseline after the fix -- paging on-call.")
        )
        return self._send(subject, body)
