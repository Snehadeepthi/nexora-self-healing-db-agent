# Runbook Storage -- a durable, human-browsable copy of the same incident
# postmortems and remediation runbooks the dashboard's Knowledge Base /
# Runbook Storage panels render inline (static/dashboard.html). The
# dashboard keeps its own inline copy so the demo still works if this
# bucket is briefly unreachable, but THIS bucket is the actual "separate
# storage" a real ops team would point runbook tooling at.

resource "google_storage_bucket" "runbooks" {
  project                     = var.project_id
  name                        = "${var.project_id}-nexora-runbooks"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true

  versioning {
    enabled = true
  }

  cors {
    origin          = ["*"]
    method          = ["GET"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }

  depends_on = [google_project_service.apis]
}

resource "google_storage_bucket_iam_member" "runbooks_public_read" {
  bucket = google_storage_bucket.runbooks.name
  role   = "roles/storage.objectViewer"
  member = "allUsers"
}

output "runbooks_bucket_console_url" {
  value       = "https://console.cloud.google.com/storage/browser/${google_storage_bucket.runbooks.name}"
  description = "Browsable GCS console link for the runbook/KB storage bucket."
}

output "runbooks_bucket_name" {
  value       = google_storage_bucket.runbooks.name
  description = "Pass to `gsutil cp` when uploading/updating the kb-incidents.json / runbooks.json objects."
}