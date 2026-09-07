"""
notifications.py
Email notifications for the two moments this pipeline needs a human to
actually notice something happened:

  1. Tier 3 actions awaiting approval (used by act.py) -- an email channel
     for the approval gate described in Step 6 of the implementation guide,
     alongside (or instead of) the Slack webhook shown there.
  2. Post-resolution status reports (used by learn.py) -- once a post-fix
     health check confirms an anomaly cleared (or failed to clear), a
     summary email goes out with what happened, what was done, and how long
     it took (MTTD/MTTR pulled straight from the audit log).

This module simulates sending mail locally: it records each message in
`self.outbox` and prints a one-line summary, so the reference pipeline has no
external dependency and no SMTP credentials to configure. Swap `_send()`'s
body for a real call to smtplib / SendGrid / your org's mail API to go to
production -- every caller and every template stays exactly the same.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import config


@dataclass
class SentEmail:
    to: str
    subject: str
    body: str
    sent_at: str


class EmailNotifier:
    def __init__(self, approvals_to=None, reports_to=None):
        self.approvals_to = approvals_to or config.APPROVAL_NOTIFY_EMAIL
        self.reports_to = reports_to or config.STATUS_REPORT_EMAIL
        self.outbox = []  # list[SentEmail] -- stands in for a real mail transport

    def _send(self, to: str, subject: str, body: str) -> SentEmail:
        """Reference implementation: records the email instead of sending it.

        To go to production, replace this method's body with something like:

            from email.mime.text import MIMEText
            import smtplib

            message = MIMEText(body)
            message["To"], message["From"], message["Subject"] = (
                to, "sre-agent@yourcompany.com", subject,
            )
            with smtplib.SMTP("smtp.yourcompany.com") as server:
                server.send_message(message)

        or call a transactional email API instead of raw SMTP. Every caller
        of send_approval_request()/send_status_report() is unaffected.
        """
        email = SentEmail(
            to=to, subject=subject, body=body,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
        self.outbox.append(email)
        print(f"  [email -> {to}] {subject}")
        return email

    # ---- Risk 1 / Step 6: Tier 3 approval notification ----
    def send_approval_request(self, action_payload: dict, action) -> SentEmail:
        subject = f"[SRE Agent] Approval needed: {action.action_key} (Tier {int(action.tier)})"
        body = (
            f"An automated diagnosis proposes a Tier {int(action.tier)} action that "
            f"requires human approval before it will run.\n\n"
            f"Incident:     {action_payload.get('incident_id')}\n"
            f"Action:       {action.action_key} -- {action.description}\n"
            f"Parameters:   {action_payload.get('params')}\n"
            f"Statement:    {action.statement_template}\n\n"
            f"Approve via the SRE Agent console, or call "
            f"executor.approve('{action_payload.get('incident_id')}') / "
            f"orchestrator.approve_and_resolve('{action_payload.get('incident_id')}') "
            f"directly."
        )
        return self._send(self.approvals_to, subject, body)

    # ---- Tier 3 approval TTL expiry notification ----
    def send_approval_timeout(self, action_payload: dict, detail: str) -> SentEmail:
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
        return self._send(self.approvals_to, subject, body)

    # ---- Risk 3 / Risk 8: post-resolution status report ----
    def send_status_report(self, incident_id: str, action_payload: dict,
                            audit_log, healthy: bool) -> SentEmail:
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
        return self._send(self.reports_to, subject, body)
