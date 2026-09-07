# Reliability safeguards: Cloud Monitoring alerting on the two native
# signals that need zero application code changes to observe --
#   1. Cloud Scheduler's own execution outcome for self-healing-agent-tick
#      (cloudrun.tf) -- since Scheduler already calls /tick every 60s with
#      an authenticated OIDC token, its own success/failure count IS "is the
#      whole agent reachable" without needing a separate uptime check (which
#      can't easily authenticate against this IAM-private Cloud Run
#      service -- see cloudrun.tf's top comment on why it's never public).
#   2. Cloud Run's own request_count metric, filtered to 5xx responses, for
#      the orchestrator service itself.
# Both metrics are emitted automatically by GCP for every real request that
# already happens -- no new instrumentation, no new failure surface.

resource "google_monitoring_notification_channel" "email" {
  display_name = "NEXORA reliability alerts"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
  depends_on = [google_project_service.apis]
}

resource "google_logging_metric" "scheduler_tick_failures" {
  name        = "nexora_scheduler_tick_failures"
  description = "Counts non-200 AttemptFinished log entries for the self-healing-agent-tick Cloud Scheduler job -- i.e. every time a tick failed to reach /tick successfully."
  filter = join(" AND ", [
    "resource.type=\"cloud_scheduler_job\"",
    "resource.labels.job_id=\"${google_cloud_scheduler_job.tick.name}\"",
    "jsonPayload.\"@type\"=\"type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished\"",
    "NOT httpRequest.status=200",
  ])
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "scheduler_tick_failures" {
  display_name = "NEXORA - Scheduler tick failures (self-healing-agent-tick)"
  combiner     = "OR"
  documentation {
    content   = "Cloud Scheduler's self-healing-agent-tick job failed to reach the orchestrator's /tick endpoint (non-200 response). Since this job fires every 60 seconds and is the agent's primary heartbeat, repeated failures mean the whole self-healing loop has stopped running -- check Cloud Run service health and the Oracle VM first (see the dashboard's Knowledge Base entry kb-01/kb-02 for the two most common causes)."
    mime_type = "text/markdown"
  }
  conditions {
    display_name = "tick execution failures in a 5-minute window"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_scheduler_job\"",
        "metric.type=\"logging.googleapis.com/user/${google_logging_metric.scheduler_tick_failures.name}\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_COUNT"
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  alert_strategy {
    auto_close = "1800s"
  }
  depends_on = [google_project_service.apis, google_logging_metric.scheduler_tick_failures]
}

resource "google_monitoring_alert_policy" "orchestrator_5xx" {
  display_name = "NEXORA - Orchestrator 5xx error rate"
  combiner     = "OR"
  documentation {
    content   = "The self-healing-orchestrator Cloud Run service is returning 5xx errors. This means requests ARE reaching it (unlike a Scheduler-tick-failure alert, which can also mean the network path itself is broken) but it's failing internally -- check Cloud Run logs first: gcloud run services logs read self-healing-orchestrator --region=${var.region}"
    mime_type = "text/markdown"
  }
  conditions {
    display_name = "5xx responses in a 5-minute window"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type=\"cloud_run_revision\"",
        "resource.labels.service_name=\"${google_cloud_run_v2_service.orchestrator.name}\"",
        "metric.type=\"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class=\"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_COUNT"
      }
      trigger {
        count = 1
      }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  alert_strategy {
    auto_close = "1800s"
  }
  depends_on = [google_project_service.apis]
}

output "monitoring_alerts_note" {
  value       = "Alerts route to ${var.alert_email}. Confirm the subscription -- Cloud Monitoring sends a one-time email confirmation link to new notification channels that must be clicked before alerts actually deliver."
  description = "Reminder: email notification channels require a one-time confirmation click."
}