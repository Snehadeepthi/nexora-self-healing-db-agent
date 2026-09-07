# AlloyDB for PostgreSQL -- the second database engine in the multi-target
# self-healing demo (see gcp_deploy/services/orchestrator/db_registry.py).
# Reuses the SAME VPC the Oracle VM is on (self-healing-agent-vpc) so the
# single shared MCP Toolbox instance running on that VM can reach both
# databases without a second Toolbox deployment -- Toolbox just gets a
# second `kind: source` block in tools.yaml (task #29) pointing at this
# cluster's private IP. Unlike Oracle (which had to move off Cloud Run's
# Serverless VPC Access connector because it corrupted the O5LOGON auth
# handshake -- see database.tf), AlloyDB speaks standard PostgreSQL wire
# protocol, so that failure mode doesn't apply here; it's on the VM anyway
# purely to share the one existing Toolbox instance rather than stand up a
# second one on Cloud Run.
#
# COST NOTE: unlike the Oracle VM (a few dollars for the whole demo
# window), a 2 vCPU / 8 GiB primary instance runs roughly $0.22/hr
# (~$26 if left running for 5 days) -- see cloud.google.com/alloydb/pricing.
# Worth stopping/deleting after the demo rather than leaving it running.
#
# DEMO SHORTCUT: deletion_protection is false on both resources below so
# `terraform destroy`/iteration doesn't fight Google's default safety
# lock. Turn it back on before anything resembling real data goes in here.
#
# No new firewall rule is needed for the Oracle VM to reach this: Private
# Services Access ingress is managed on Google's side of the peering, and
# this VPC has no restrictive egress rules (see network.tf), so the
# default allow-all-egress already covers it.

# Reserved range for VPC peering -- AlloyDB (like Cloud SQL) connects via
# Private Services Access, not a normal subnet IP. address is left unset
# so Google auto-allocates a /16 that doesn't collide with the existing
# 10.10.0.0/24 subnet or 10.10.10.0/28 connector range; apply fails loudly
# rather than silently misconfiguring if that were ever not true.
resource "google_compute_global_address" "alloydb_private_ip_alloc" {
  name          = "alloydb-private-ip-alloc"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "alloydb_vpc_connection" {
  network  = google_compute_network.vpc.id
  service  = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [
    google_compute_global_address.alloydb_private_ip_alloc.name,
    google_compute_global_address.mysql_private_ip_alloc.name,
  ]
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "alloydb_password" {
  secret_id = "alloydb-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "alloydb_password_v1" {
  secret      = google_secret_manager_secret.alloydb_password.id
  secret_data = var.alloydb_password
}

resource "google_alloydb_cluster" "primary" {
  cluster_id = "self-healing-alloydb"
  location   = var.region
  network_config {
    network = google_compute_network.vpc.id
  }
  initial_user {
    user     = "postgres"
    password = var.alloydb_password
  }
  deletion_protection = false
  depends_on          = [google_service_networking_connection.alloydb_vpc_connection]
}

resource "google_alloydb_instance" "primary" {
  cluster       = google_alloydb_cluster.primary.name
  instance_id   = "self-healing-alloydb-primary"
  instance_type = "PRIMARY"
  machine_config {
    cpu_count = 2
  }
}
