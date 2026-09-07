# Cloud SQL for MySQL -- third database engine (task #33).
# Mirrors alloydb.tf's pattern: private-IP-only, riding the same VPC peering
# connection (extended in alloydb.tf to reserve a second range for this).

resource "google_compute_global_address" "mysql_private_ip_alloc" {
  name          = "mysql-private-ip-alloc"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_secret_manager_secret" "mysql_password" {
  secret_id = "mysql-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "mysql_password_v1" {
  secret      = google_secret_manager_secret.mysql_password.id
  secret_data = var.mysql_password
}

resource "google_sql_database_instance" "mysql" {
  name                = "self-healing-mysql"
  database_version    = "MYSQL_8_0"
  region              = var.region
  deletion_protection = false # DEMO SHORTCUT, same convention as alloydb.tf -- allows terraform destroy without manual unprotect. Revisit before real prod use.

  settings {
    tier              = "db-custom-2-8192"
    availability_type = "ZONAL"

    # performance_schema is OFF by Cloud SQL default -- required for
    # mysql_find_blocking_session / mysql_poll_telemetry's blocking and
    # idle-in-transaction detection (performance_schema.data_lock_waits,
    # .threads, .events_transactions_current all silently return empty
    # rows with the instrumentation engine off, MySQL 8.0 has no
    # INFORMATION_SCHEMA.INNODB_LOCK_WAITS fallback -- it was removed).
    # Turning this on live via `gcloud sql instances patch` restarts the
    # instance, so it's captured here too to avoid drift on the next apply.
    database_flags {
      name  = "performance_schema"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
    }

    backup_configuration {
      enabled = false
    }
  }

  depends_on = [google_service_networking_connection.alloydb_vpc_connection]
}

resource "google_sql_database" "app_db" {
  name     = "selfhealing"
  instance = google_sql_database_instance.mysql.name
}

resource "google_sql_user" "app_user" {
  name     = "app_user"
  instance = google_sql_database_instance.mysql.name
  password = var.mysql_password
}
