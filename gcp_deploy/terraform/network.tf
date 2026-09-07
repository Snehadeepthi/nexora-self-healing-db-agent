# Private networking: the Oracle VM has no public IP. Cloud Run reaches it
# only through a Serverless VPC Access connector; SSH access (for the
# documented restart_listener follow-up, or general admin) goes through
# Identity-Aware Proxy TCP forwarding rather than an open port.

resource "google_compute_network" "vpc" {
  name                    = "self-healing-agent-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "self-healing-agent-subnet"
  ip_cidr_range = "10.10.0.0/24"
  region        = var.region
  network       = google_compute_network.vpc.id
}

resource "google_vpc_access_connector" "connector" {
  name           = "sha-vpc-connector"
  region         = var.region
  network        = google_compute_network.vpc.name
  ip_cidr_range  = "10.10.10.0/28"
  min_throughput = 200
  max_throughput = 300
  depends_on     = [google_project_service.apis]
}

resource "google_compute_firewall" "allow_oracle_from_connector" {
  name    = "allow-oracle-from-connector"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["1521", "5000", "5001"]
  }

  source_ranges = ["10.10.10.0/28", "10.10.0.0/24"]
  target_tags   = ["oracle-db"]
}

# IAP TCP forwarding range -- lets you `gcloud compute ssh` the Oracle VM
# without it ever having a public IP or an open port 22 to the internet.
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "allow-iap-ssh"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["oracle-db"]
}

# The Oracle VM has no external IP (by design), which means without a NAT
# gateway it has no route to the public internet at all -- not even to pull
# its own startup container image. Confirmed via serial console: konlet-
# startup retried gcr.io/v2/ forever with "context deadline exceeded" and
# never succeeded, so gvenzl/oracle-xe never started and Oracle never bound
# port 1521 -- this is what Toolbox's "dial tcp 10.10.0.2:1521: i/o timeout"
# was actually reporting. Cloud NAT gives instances without external IPs
# outbound internet access while staying unreachable from the internet
# inbound -- private-by-construction is unchanged, this only opens egress.
resource "google_compute_router" "router" {
  name    = "self-healing-agent-router"
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = "self-healing-agent-nat"
  router                             = google_compute_router.router.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}
