"""
learn.py
Local/dev version of the Learn stage (Step 7).

Risk 4 mitigation: a genuinely novel incident (cosine distance beyond
config.NOVELTY_THRESHOLD from anything already known) is submitted to
`RunbookStore.pending_review`, NOT inserted directly into the searchable
store. It only becomes usable by reason.py after a human calls
`approve_pending_runbook()`.

Email notification: as soon as the post-fix health check result is known
(healthy or not), a status report goes out via notifications.EmailNotifier
summarizing what happened and how long it took (MTTD/MTTR from audit.py).
This fires whether or not the incident turns out to be healthy -- an
unhealthy result is exactly the case where the team most needs to hear from
the agent.
"""

import config
from runbooks import Runbook


def learn_from_incident(event: dict, action_payload: dict, healthy: bool,
                         runbook_store, audit_log, notifier=None):
    incident_id = event.get("incident_id")

    if notifier is not None:
        email = notifier.send_status_report(incident_id, action_payload, audit_log, healthy)
        audit_log.log("status_report_sent", incident_id=incident_id, to=email.to,
                       healthy=healthy)

    if not healthy:
        audit_log.log(
            "post_fix_unhealthy", incident_id=incident_id,
            detail="fix applied but telemetry did not recover -- paging on-call",
        )
        return

    query_text = event.get("query_text", "")
    nearest_distance = runbook_store.nearest_distance_including_pending(query_text)
    ingested = nearest_distance > config.NOVELTY_THRESHOLD

    if ingested:
        runbook = Runbook(
            title=f"{action_payload['action_key']}::{incident_id}",
            body=query_text,
            resolution_action_key=action_payload["action_key"],
        )
        runbook_store.submit_for_review(runbook)  # Risk 4: review queue, not auto-live

    audit_log.log(
        "learning_event", incident_id=incident_id, nearest_distance=nearest_distance,
        ingested=ingested,
        status="pending_review" if ingested else "duplicate_of_existing",
    )
