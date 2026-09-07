# The orchestrator Cloud Run service is never made public: ingress is the
# default (ALL, i.e. it has a normal HTTPS URL) but no principal except the
# Cloud Scheduler service account below is granted roles/run.invoker -- so
# IAM, not network topology, is what keeps it private. Grant yourself
# roles/run.invoker (see README_DEPLOY.md) to curl it manually for testing.

resource "google_artifact_registry_repository" "repo" {
  location      = var.region
  repository_id = "self-healing-agent"
  format        = "DOCKER"
  depends_on    = [google_project_service.apis]
}

resource "google_cloud_run_v2_service" "orchestrator" {
  name                = "self-healing-orchestrator"
  location            = var.region
  deletion_protection = false

  template {
    service_account = google_service_account.orchestrator_sa.email

    # Single warm instance, one request at a time -- see main.py's module
    # docstring for why: the consecutive-anomaly counter and the Tier 3
    # pending_approvals queue are in-process state, not persisted anywhere.
    scaling {
      min_instance_count = 1
      max_instance_count = 1
    }
    max_instance_request_concurrency = 4

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      # Bootstrap placeholder only. Cloud Run requires an image to already
      # exist at service-creation time, but our real image doesn't exist
      # until Cloud Build pushes it (which happens AFTER this `terraform
      # apply` succeeds) -- classic chicken-and-egg. We point at Google's
      # public quickstart image here just so the first `apply` has something
      # valid to create, then `lifecycle.ignore_changes` below tells
      # Terraform to never again touch this field -- cloudbuild.yaml's
      # deploy-image step (`gcloud run services update --image=...`) owns
      # the real image from that point on, and re-running `terraform apply`
      # won't stomp on it back to this placeholder.
      image = "us-docker.pkg.dev/cloudrun/container/hello"

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }
      # ADK/google-genai's own env-var convention for routing Gemini calls
      # through Vertex AI (Application Default Credentials, this service's
      # own SA) rather than the Gemini Developer API -- no manual
      # vertexai.init() call needed, unlike the pre-ADK llm_client.py.
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "TRUE"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      # The orchestrator no longer talks to Oracle directly -- only
      # mcp_toolbox.tf's Toolbox service does. TOOLBOX_URL points at it, and
      # TOOLBOX_REQUIRE_AUTH=true tells db_tools.py to attach an OIDC
      # identity token to every call (see mcp_toolbox.tf's IAM comment).
      env {
        name  = "TOOLBOX_URL"
        value = "http://${google_compute_instance.oracle_db.network_interface[0].network_ip}:5000"
      }
      env {
        name  = "TOOLBOX_REQUIRE_AUTH"
        value = "false"
      }
      env {
        name  = "TOOLBOX_URL_ALLOYDB_MYSQL"
        value = "https://toolbox-alloydb-mysql-casezd3hrq-uc.a.run.app"
      }
      env {
        name  = "SLACK_WEBHOOK_SECRET"
        value = google_secret_manager_secret.slack_webhook.secret_id
      }
      env {
        name  = "DEMO_TRIGGER_URL"
        value = "http://${google_compute_instance.oracle_db.network_interface[0].network_ip}:5001"
      }
      env {
        name  = "DEMO_TRIGGER_SECRET"
        value = "demo-trigger-secret"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

    }
  }

  # See the comment on `image` above -- Cloud Build owns the deployed image
  # after the first apply; Terraform should stop trying to reconcile it.
  # client/client_version are also excluded -- every `gcloud builds submit`
  # / `gcloud run deploy` we run directly against this service stamps
  # those fields, and Terraform's config never sets them, so without this
  # every plan wants to null them out. Harmless drift, just noisy.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, client, client_version]
  }

  depends_on = [google_project_service.apis]
}

resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  name     = google_cloud_run_v2_service.orchestrator.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_sa.email}"
}

resource "google_cloud_scheduler_job" "tick" {
  name     = "self-healing-agent-tick"
  region   = var.region
  schedule = "* * * * *" # every minute -- Cloud Scheduler's floor; the guide's 30s cadence is Pub/Sub-push, not cron-based

  http_target {
    uri         = "${google_cloud_run_v2_service.orchestrator.uri}/tick"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }

  depends_on = [google_project_service.apis]
}
# AlloyDB's autonomous tick -- mirrors the Oracle job above exactly (same
# schedule, same scheduler SA), just targets ?db=alloydb. Added only after
# main.py's /tick?db=alloydb was manually verified end-to-end (Sense,
# Predict, and both Tier 2 auto-exec and Tier 3 approval-gated Reason/Act)
# against the live instance -- see task #32's verification. Deliberately
# a SEPARATE resource rather than a for_each over db_registry's engines:
# this project's Terraform state has known drift on database.tf relative
# to the live Oracle VM (task #36, unresolved), so every apply here must
# stay narrowly -target-scoped -- a for_each refactor of an existing
# resource risks a destroy/recreate plan on unrelated resources whose
# state Terraform hasn't reconciled cleanly. Revisit consolidating this
# once #36 is resolved and a bare `terraform plan` is trustworthy again.
resource "google_cloud_scheduler_job" "tick_alloydb" {
  name     = "self-healing-agent-tick-alloydb"
  region   = var.region
  schedule = "* * * * *" # every minute -- same cadence as the Oracle tick job

  http_target {
    uri         = "https://self-healing-orchestrator-503897174037.us-central1.run.app/tick?db=alloydb"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }

  depends_on = [google_project_service.apis]
}

# MySQL's autonomous tick -- mirrors the AlloyDB job above exactly, same
# reasoning for the hardcoded URL rather than a
# google_cloud_run_v2_service.orchestrator.uri reference (that resource has
# known, unresolved Terraform drift -- see task #36/#42 -- so every apply
# here stays deliberately decoupled from it).
resource "google_cloud_scheduler_job" "tick_mysql" {
  name     = "self-healing-agent-tick-mysql"
  region   = var.region
  schedule = "* * * * *" # every minute -- same cadence as the other two tick jobs

  http_target {
    uri         = "https://self-healing-orchestrator-503897174037.us-central1.run.app/tick?db=mysql"
    http_method = "POST"
    oidc_token {
      service_account_email = google_service_account.scheduler_sa.email
    }
  }

  depends_on = [google_project_service.apis]
}
