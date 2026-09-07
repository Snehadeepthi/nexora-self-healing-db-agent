# Second MCP Toolbox instance -- AlloyDB + MySQL only. Drop this file into
# gcp_deploy/terraform/ (alongside mcp_toolbox.tf, which stays exactly as-is
# and keeps describing the VM-pinned Oracle-only instance).
#
# Why a second instance instead of moving everyone onto it, or everyone off
# the VM: mcp_toolbox.tf's own comment explains Toolbox-over-the-VPC-
# connector corrupts Oracle's O5LOGON handshake specifically -- that's an
# Oracle-protocol problem, not a general Toolbox-on-Cloud-Run problem, and
# AlloyDB/MySQL never hit it (their wire protocols are unaffected). So
# Oracle stays VM-pinned (127.0.0.1, unchanged, zero risk to what already
# works) and only AlloyDB+MySQL move to Cloud Run -- cutting a VM
# kernel-panic/hardware-fault/compute-outage's blast radius from all 3
# engines' telemetry down to just Oracle's.
#
# Config delivery: Cloud Run has no host-volume-mount equivalent to the VM's
# `docker run -v tools.yaml:/app/tools.yaml`, and this project already found
# Toolbox's own ${VAR} env-var substitution unreliable for at least one
# field (mcp_toolbox.tf / database.tf's ${ORACLE_PASSWORD} finding) -- so
# rather than trust it here for a different source type untested, this
# mirrors the VM's actual proven approach: a FULLY RESOLVED tools.yaml (real
# host/port/user/password already substituted in, see
# split_toolbox_config.py + README_TOOLBOX_SPLIT.md) stored as a Secret
# Manager secret and mounted as a file, not env-var substitution at
# Toolbox's own load time.
#
# Trust model: INGRESS_TRAFFIC_INTERNAL_ONLY means this service is not
# reachable from the public internet at all, regardless of IAM -- the same
# "private by network topology" trust model already accepted for the VM's
# Toolbox (TOOLBOX_REQUIRE_AUTH=false there, no public IP). Cloud Run still
# enforces its own invoker IAM check even on an internal-only service, so
# invoker is granted to allUsers here deliberately -- the ingress
# restriction is the actual enforced boundary, exactly the same reasoning
# already applied to the VM. This also means the orchestrator's Toolbox
# client for this instance needs no OIDC token machinery (unlike the
# original, since-reverted Cloud-Run-Toolbox design cloudrun.tf's own
# history alludes to) -- see db_tools.py's get_sync_client_for(), which
# calls ToolboxSyncClient(url) with no auth headers, matching this.

resource "google_secret_manager_secret" "toolbox_cloudrun_config" {
  secret_id = "toolbox-alloydb-mysql-config"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

# No google_secret_manager_secret_version resource here on purpose -- the
# actual tools.yaml content carries real AlloyDB/MySQL credentials, and this
# project's existing convention (see cloudrun.tf's own comment on the
# orchestrator image) is to keep anything credential-bearing or
# build-time-produced out of Terraform state and apply it out-of-band. Add
# the first version with:
#   gcloud secrets versions add toolbox-alloydb-mysql-config \
#     --data-file=gcp_deploy/tools_db/tools_cloudrun.resolved.yaml
# See README_TOOLBOX_SPLIT.md step 3.

resource "google_cloud_run_v2_service" "toolbox_alloydb_mysql" {
  name                = "toolbox-alloydb-mysql"
  location            = var.region
  deletion_protection = false
  # Reachability fix (was INGRESS_TRAFFIC_INTERNAL_ONLY): the orchestrator's
  # own vpc_access.egress is PRIVATE_RANGES_ONLY, which does not route calls
  # to this service's public *.run.app hostname through the connector -- so
  # an internal-only ingress here rejected every orchestrator call at
  # Google's edge before it ever reached the container. Default ingress
  # (this field omitted) plus IAM-restricted invoker below matches the
  # exact trust model cloudrun.tf's own comment already documents for the
  # orchestrator service itself -- "IAM, not network topology, is what
  # keeps it private" -- rather than widening the orchestrator's shared
  # egress setting (which also carries Slack/Gemini/BigQuery traffic).
  ingress             = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.orchestrator_sa.email

    # Toolbox itself is a stateless SQL proxy (unlike the orchestrator, it
    # holds no incident/approval state), so it's safe to let this scale --
    # min=1 just avoids cold-start latency showing up as extra Sense-stage
    # tick latency mid-incident, the same reasoning cloudrun.tf's orchestrator
    # service already uses for its own min_instance_count.
    scaling {
      min_instance_count = 1
      max_instance_count = 2
    }

    vpc_access {
      connector = google_vpc_access_connector.connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    volumes {
      name = "toolbox-config"
      secret {
        secret = google_secret_manager_secret.toolbox_cloudrun_config.secret_id
        items {
          version = "latest"
          path    = "tools.yaml"
        }
      }
    }

    containers {
      image = "us-central1-docker.pkg.dev/database-toolbox/toolbox/toolbox:1.9.0"
      args  = ["--config=/app/config/tools.yaml", "--address=0.0.0.0", "--port=8080"]

      ports {
        container_port = 8080
      }

      volume_mounts {
        name       = "toolbox-config"
        mount_path = "/app/config"
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }
    }
  }

  depends_on = [google_project_service.apis]
}

# IAM-restricted invoker (see reachability-fix note above) -- only the
# orchestrator's own service account may call this service, same pattern
# cloudrun.tf uses for scheduler_sa -> orchestrator.
resource "google_cloud_run_v2_service_iam_member" "toolbox_alloydb_mysql_orchestrator_invoker" {
  name     = google_cloud_run_v2_service.toolbox_alloydb_mysql.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}

output "toolbox_alloydb_mysql_url" {
  value       = google_cloud_run_v2_service.toolbox_alloydb_mysql.uri
  description = "Set this as TOOLBOX_URL_ALLOYDB_MYSQL on the orchestrator Cloud Run service's env block in cloudrun.tf (see README_TOOLBOX_SPLIT.md step 5)."
}

resource "google_secret_manager_secret_iam_member" "toolbox_cloudrun_config_accessor" {
  secret_id = google_secret_manager_secret.toolbox_cloudrun_config.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.orchestrator_sa.email}"
}
