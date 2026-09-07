# Scoped service accounts -- the orchestrator Cloud Run service gets exactly
# what it needs (BigQuery on the db_ops dataset only, Vertex AI User, read
# access to its two secrets) and nothing else. It never gets a broad
# project-wide Editor/Owner role.

data "google_project" "current" {
  project_id = var.project_id
}

# cloudbuild.yaml pushes to Artifact Registry and updates the Cloud Run
# service's image. Which identity actually RUNS the build depends on the
# project: older projects default to the legacy Cloud Build SA
# (PROJECT_NUMBER@cloudbuild.gserviceaccount.com); newer ones default to the
# Compute Engine default SA (PROJECT_NUMBER-compute@developer.gserviceaccount.com)
# instead, per a Google-side default-behavior change. Rather than guess which
# one applies, grant both -- the unused grant is harmless.
resource "google_project_iam_member" "cloudbuild_artifact_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "cloudbuild_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_project_iam_member" "compute_default_sa_artifact_writer" {
  project = var.project_id
  role    = "roles/artifactregistry.writer"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "compute_default_sa_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

# Needed so the build can read back the source tarball it just uploaded to
# the Cloud Build staging bucket -- the specific gap behind the
# "storage.objects.get denied" error on newer projects.
resource "google_project_iam_member" "compute_default_sa_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_project_iam_member" "compute_default_sa_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_service_account" "orchestrator_sa" {
  account_id   = "sha-orchestrator-sa"
  display_name = "Self-Healing Agent - Orchestrator Cloud Run"
}

# Deploying a Cloud Run revision that runs AS orchestrator_sa requires the
# deployer to be able to actAs that service account, not just have
# run.admin generally -- confirmed by cloudbuild.yaml's deploy-image step
# failing with "PERMISSION_DENIED: Permission 'iam.serviceaccounts.actAs'
# denied on service account sha-orchestrator-sa@...". Same
# grant-both-possible-identities reasoning as the run.admin/
# artifactregistry.writer grants above -- scoped to this one service
# account rather than project-wide, so the deploying identity can impersonate
# nothing else.
resource "google_service_account_iam_member" "cloudbuild_orchestrator_sa_user" {
  service_account_id = google_service_account.orchestrator_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_project.current.number}@cloudbuild.gserviceaccount.com"
}

resource "google_service_account_iam_member" "compute_default_sa_orchestrator_sa_user" {
  service_account_id = google_service_account.orchestrator_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${data.google_project.current.number}-compute@developer.gserviceaccount.com"
}

resource "google_service_account" "scheduler_sa" {
  account_id   = "sha-scheduler-sa"
  display_name = "Self-Healing Agent - Cloud Scheduler invoker"
}

resource "google_project_iam_member" "orchestrator_vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

resource "google_project_iam_member" "orchestrator_bq_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

resource "google_bigquery_dataset_iam_member" "orchestrator_bq_editor" {
  dataset_id = google_bigquery_dataset.db_ops.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

# NOTE: the orchestrator no longer reads the Oracle password secret itself
# -- only mcp_toolbox.tf's Toolbox service talks to Oracle now (see
# toolbox_oracle_secret in mcp_toolbox.tf), so there is deliberately no
# oracle_password grant for orchestrator_sa here. Same least-privilege
# reasoning this file's header comment already states.

resource "google_secret_manager_secret" "slack_webhook" {
  secret_id = "sre-agent-slack-webhook"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "slack_webhook_v1" {
  secret      = google_secret_manager_secret.slack_webhook.id
  secret_data = var.notify_slack_webhook_url != "" ? var.notify_slack_webhook_url : "https://hooks.slack.com/services/REPLACE/ME/LATER"
}

resource "google_secret_manager_secret_iam_member" "orchestrator_slack_secret" {
  secret_id = google_secret_manager_secret.slack_webhook.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

resource "google_secret_manager_secret_iam_member" "orchestrator_demo_trigger_secret" {
  secret_id = "demo-trigger-secret"
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
