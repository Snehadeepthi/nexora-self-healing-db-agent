# Oracle Database XE (free edition) on a Compute Engine VM, running as a
# container on Container-Optimized OS via the standard "container on GCE"
# metadata pattern. This is the documented stand-in for the real Oracle
# Database@Google Cloud partner product (Exadata-based, requires a separate
# Oracle/Google enrollment) -- it keeps act.py's exact SQL statements
# ("ALTER SYSTEM KILL SESSION ...", etc.) and the real python-oracledb
# driver working unchanged, just against a much cheaper, self-service
# instance. Swap this file for a real Oracle Database@Google Cloud
# connection string when that enrollment is in place; oracle_client.py's
# interface doesn't need to change either way.
#
# Known shortcut: the DB password is passed via instance metadata (readable
# by anyone with compute.instances.get on this VM) rather than having the
# container fetch it from Secret Manager at boot. Fine for a demo; harden
# before real production traffic by giving the container entrypoint a
# Secret Manager read instead.

resource "google_service_account" "oracle_vm_sa" {
  account_id   = "oracle-db-vm-sa"
  display_name = "Self-Healing Agent - Oracle DB VM"
}

resource "google_secret_manager_secret" "oracle_password" {
  secret_id = "oracle-db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "oracle_password_v1" {
  secret      = google_secret_manager_secret.oracle_password.id
  secret_data = var.oracle_db_password
}

resource "google_compute_disk" "oracle_data" {
  name = "self-healing-oracle-data"
  zone = var.zone
  type = "pd-balanced"
  size = 20

  # Deliberately its own resource, independent of the VM instance -- if the
  # instance ever needs to be replaced (e.g. a future machine-type change),
  # this disk and the real Oracle data on it survive that untouched and
  # just get reattached.
}

resource "google_compute_instance" "oracle_db" {
  name         = "self-healing-oracle-db"
  machine_type = "e2-medium"
  zone         = var.zone
  tags         = ["oracle-db"]

  boot_disk {
    initialize_params {
      image = "projects/cos-cloud/global/images/family/cos-stable"
      size  = 40
      type  = "pd-balanced"
    }
  }

  # Persistent disk for Oracle's data directory -- see the
  # metadata_startup_script comment below for why (previously oracle-xe had
  # no persistent volume at all and reinitialized from scratch on every
  # boot). device_name here is what fixes the device's stable path on the
  # guest as /dev/disk/by-id/google-oracle-data, referenced by that script.
  attached_disk {
    source      = google_compute_disk.oracle_data.self_link
    device_name = "oracle-data"
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    # deliberately no access_config block -- no public IP
  }

  service_account {
    email  = google_service_account.oracle_vm_sa.email
    scopes = ["cloud-platform"]
  }

  # metadata_startup_script is intentionally excluded from drift detection.
  # The live VM's startup script has been iterated on directly over SSH
  # since creation (Aug 21) rather than via `terraform apply` -- this field
  # is ForceNew in the provider, so tracking it here would make every plan
  # try to destroy-and-recreate the running Oracle VM. The live script is
  # treated as authoritative; remove this block deliberately if the .tf
  # template is ever reconciled and a real redeploy is intended.
  lifecycle {
    ignore_changes = [metadata_startup_script]
  }

  metadata = {
    # oracle-xe is no longer declared here (Konlet) -- see
    # metadata_startup_script below, which launches it directly via
    # `docker run` instead, the same pattern already used for toolbox, so
    # boot ordering relative to the persistent-disk mount is guaranteed
    # rather than left to Konlet's own (previously-confirmed unreliable)
    # startup sequencing.
    enable-oslogin = "TRUE"
  }

  labels = {
    container-vm = "cos-stable"
  }

  # MCP Toolbox for Databases, run as a plain sibling container on this same
  # VM instead of as a separate Cloud Run service. This is a deliberate
  # pivot: Toolbox-on-Cloud-Run, reaching this VM through either the
  # Serverless VPC Access connector or Direct VPC Egress, was empirically
  # confirmed to corrupt Oracle's binary O5LOGON auth handshake in transit --
  # the exact same official image, exact same credentials, connecting over
  # --network host on this VM instead, worked immediately. Toolbox here talks
  # to Oracle over 127.0.0.1, so that whole network hop -- and whatever was
  # breaking it -- is gone. The orchestrator (still on Cloud Run) now reaches
  # Toolbox over plain HTTP through the connector instead, a far more
  # tolerant protocol than Oracle's own wire format.
  #
  # Deliberately NOT managed via gce-container-declaration/Konlet alongside
  # oracle-xe above: startup-script re-runs on every boot and is trivially
  # idempotent (docker rm -f + docker run), which sidesteps having to reason
  # about Konlet's multi-container ordering/networking guarantees. `docker
  # run --restart=always` keeps it running (and retrying, if it starts
  # before Oracle's own init finishes) across reboots.
  #
  # Same known shortcut as the ORACLE_PASSWORD above: the password is
  # embedded in instance metadata, readable by anyone with
  # compute.instances.get on this VM. Fine for a demo.
  #
  # tools.yaml's source block still uses ${ORACLE_HOST}/${ORACLE_PORT}/etc.
  # placeholders (kept as-is for portability/documentation), but they are
  # NOT resolved by Toolbox's own --config-time env-var substitution --
  # empirically confirmed (v1.9.0, the current release as of this writing)
  # that ${ORACLE_PASSWORD} specifically is left unresolved by Toolbox at
  # runtime regardless of which env var name backs it, while the other
  # fields in the same block substitute fine. Isolated by testing every
  # combination directly on this VM: raw go-ora and sqlplus both
  # authenticate instantly with the real password; a hand-built tools.yaml
  # with every value hardcoded works instantly via Toolbox too; only
  # Toolbox's own ${ORACLE_PASSWORD} resolution path does not, and always
  # produces the same ORA-01017 no matter the network path (Cloud Run
  # connector, Direct VPC Egress, or plain loopback on this same VM) --
  # ruling out network, credentials, and go-ora driver version (identical
  # v2.9.0 pinned both in Toolbox and in the raw reproduction). Rather than
  # depend on a specific Toolbox release's env-var handling, resolve the
  # placeholders ourselves in bash before Toolbox ever reads the file.
  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -e

    # --- Host-level firewall -------------------------------------------------
    # Discovered live: removing gce-container-declaration (Konlet) entirely
    # below -- the right call for the boot-ordering reason explained further
    # down -- also removes whatever Konlet's own startup sequence was doing
    # to this host's iptables as a side effect of managing ANY container via
    # that metadata key. COS's bare default INPUT policy is DROP, with only
    # loopback/ICMP/established/SSH explicitly allowed (confirmed via `sudo
    # iptables -L INPUT -n -v` after the first boot without Konlet, which
    # showed exactly that) -- so even though the real VPC firewall
    # (network.tf's allow_oracle_from_connector) correctly permits the
    # connector's traffic on 1521/5000, this host-level chain silently
    # dropped it anyway, surfacing only as a generic "Connection timeout"
    # from the orchestrator with no other clue. Re-running this on every
    # boot is a harmless idempotent append (the -C check skips it if the
    # rule's already there).
    for port in 1521 5000 5001; do
      if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        iptables -A INPUT -p tcp --dport "$port" -j ACCEPT
      fi
    done

    # --- Persistent disk for Oracle's data directory ------------------------
    # Reliability safeguard: previously oracle-xe was declared via
    # gce-container-declaration (Konlet) with no persistent volume, so its
    # entire /opt/oracle/oradata lived in the container's own ephemeral
    # writable layer -- wiped and reinitialized from scratch on every single
    # VM boot (confirmed live: several minutes of downtime plus a listener-
    # registration race, on every restart, planned or not). This mounts a
    # real, separate persistent disk (google_compute_disk.oracle_data,
    # attached above) at /mnt/oracle-data and bind-mounts it into the
    # container at /opt/oracle/oradata -- the exact path confirmed from this
    # VM's own alert_XE.log ("/opt/oracle/oradata/XE/redo01.log"). Formatted
    # ONLY on first-ever boot (checked via blkid, so re-running this
    # idempotently on every later boot never reformats real data).
    DISK_DEVICE="/dev/disk/by-id/google-oracle-data"
    MOUNT_POINT="/mnt/stateful_partition/oracle-data"

    for i in $(seq 1 30); do
      [ -e "$DISK_DEVICE" ] && break
      sleep 2
    done

    if ! blkid "$DISK_DEVICE" >/dev/null 2>&1; then
      mkfs.ext4 -F "$DISK_DEVICE"
    fi

    mkdir -p "$MOUNT_POINT"
    mount "$DISK_DEVICE" "$MOUNT_POINT"
    mkdir -p "$MOUNT_POINT/oradata"
    chown -R 54321:54321 "$MOUNT_POINT/oradata"

    # --- oracle-db (Oracle 23ai Free), launched directly via docker run (not Konlet) ---
    # Moved off gce-container-declaration for the same reason toolbox
    # already was (see the comment on the toolbox docker run below): Konlet's
    # boot ordering relative to this script isn't guaranteed, and oracle-db
    # now depends on the persistent-disk mount above having already
    # happened. A plain docker run here, after the mount, removes that race
    # entirely -- and as a side benefit, the container now has a fixed,
    # known name ("oracle-xe") instead of Konlet's random "klt-oracle-xe-*"
    # suffix, so the grant script below no longer needs to look its name up
    # (bug #1 from the old comment here is gone, not just worked around).
    docker rm -f oracle-db >/dev/null 2>&1 || true
    # Oracle 23ai Free (gvenzl/oracle-free), upgraded from 21c XE now that
    # the persistent disk above means this only needs to run its first-time
    # init once, not on every boot. Env var names (ORACLE_PASSWORD/APP_USER/
    # APP_USER_PASSWORD) are unchanged -- gvenzl keeps these consistent
    # across the xe/free image lines deliberately. This is a fresh 23ai
    # instance, not an in-place upgrade of the old 21c datafiles (these
    # lightweight demo images don't support Oracle's own DBUA upgrade path)
    # -- it initializes into its own FREE-named subdirectory alongside the
    # old XE-named one already on the disk, which is harmless (nothing of
    # value lived in Oracle itself; see gcp_audit.py -- the real audit
    # trail is BigQuery, not this database).
    docker run -d --name oracle-db --network host --restart=always \
      -v "$MOUNT_POINT/oradata:/opt/oracle/oradata" \
      -e ORACLE_PASSWORD="${var.oracle_db_password}" \
      -e APP_USER=executor_sa \
      -e APP_USER_PASSWORD="${var.oracle_db_password}" \
      gvenzl/oracle-free:23-slim

    mkdir -p /var/lib/toolbox
    cat > /var/lib/toolbox/tools.yaml << 'TOOLBOX_YAML_EOF'
    ${file("${path.module}/../tools_db/tools.yaml")}
    TOOLBOX_YAML_EOF

    sed -i \
      -e 's|$${ORACLE_HOST}|127.0.0.1|g' \
      -e 's|$${ORACLE_PORT}|1521|g' \
      -e 's|$${ORACLE_SERVICE}|FREEPDB1|g' \
      -e 's|$${ORACLE_USER}|executor_sa|g' \
      -e 's|$${ORACLE_PASSWORD}|${var.oracle_db_password}|g' \
      -e 's|$${MYSQL_HOST}|${google_sql_database_instance.mysql.private_ip_address}|g' \
      -e 's|$${MYSQL_PORT}|3306|g' \
      -e 's|$${MYSQL_DATABASE}|selfhealing|g' \
      -e 's|$${MYSQL_USER}|app_user|g' \
      -e 's|$${MYSQL_PASSWORD}|${var.mysql_password}|g' \
      -e 's|$${ALLOYDB_HOST}|${google_alloydb_instance.primary.ip_address}|g' \
      -e 's|$${ALLOYDB_PORT}|5432|g' \
      -e 's|$${ALLOYDB_DATABASE}|postgres|g' \
      -e 's|$${ALLOYDB_USER}|postgres|g' \
      -e 's|$${ALLOYDB_PASSWORD}|${var.alloydb_password}|g' \
      /var/lib/toolbox/tools.yaml

    docker rm -f toolbox >/dev/null 2>&1 || true
    docker run -d --name toolbox --network host --restart=always \
      -v /var/lib/toolbox/tools.yaml:/app/tools.yaml \
      us-central1-docker.pkg.dev/database-toolbox/toolbox/toolbox:latest \
      --config=/app/tools.yaml --address=0.0.0.0 --port=5000
    # --- demo-trigger listener: real staged-failure trigger for the
    # AlloyDB/MySQL Tier 2 dashboard demo buttons (#52). A tiny Alpine +
    # BusyBox httpd container, mounted against the host's docker socket
    # (docker-outside-of-docker -- same engine, no privileged/DinD needed)
    # so its CGI scripts can spawn real sibling `docker run` sessions
    # against the live databases, exactly like the simulate_*.sh scripts
    # a human would run over SSH, just reachable over HTTP from the
    # orchestrator instead. Requires X-Trigger-Secret to match
    # TRIGGER_SECRET below -- reachable only from the VPC connector range
    # (network.tf's allow_oracle_from_connector, now also covering 5001).
    mkdir -p /var/lib/demo-triggers
    cat > /var/lib/demo-triggers/idle-alloydb.cgi << 'IDLE_ALLOYDB_CGI_EOF'
    ${file("${path.module}/../demo_triggers/idle-alloydb.cgi")}
    IDLE_ALLOYDB_CGI_EOF
    cat > /var/lib/demo-triggers/idle-mysql.cgi << 'IDLE_MYSQL_CGI_EOF'
    ${file("${path.module}/../demo_triggers/idle-mysql.cgi")}
    IDLE_MYSQL_CGI_EOF
    chmod +x /var/lib/demo-triggers/*.cgi
    mkdir -p /var/lib/demo-server
    cat > /var/lib/demo-server/cgi_server.py << 'CGI_SERVER_PY_EOF'
    ${file("${path.module}/../demo_triggers/cgi_server.py")}
    CGI_SERVER_PY_EOF
    docker rm -f demo-trigger >/dev/null 2>&1 || true
    docker run -d --name demo-trigger --network host --restart=always \
      -v /var/lib/toolbox/tools.yaml:/var/lib/toolbox/tools.yaml:ro \
      -v /var/lib/demo-triggers:/www/cgi-bin \
      -v /var/lib/demo-server:/app:ro \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -e TRIGGER_SECRET="${var.demo_trigger_secret}" \
      docker:24-cli sh -c "apk add --no-cache python3 >/dev/null 2>&1 && python3 /app/cgi_server.py"

    # Same known race as before (Oracle's listener can take a few seconds
    # after "DATABASE IS READY TO USE" to register XEPDB1's service) --
    # retry the real grant command itself until its own output is clean of
    # ORA-/SP2- error codes, since sqlplus can exit 0 even when individual
    # statements inside the script failed. GRANT is idempotent, so
    # re-running this on a boot where the grants already exist (i.e. every
    # boot after the very first, now that the disk persists) is a harmless
    # no-op.
    cat > /var/lib/toolbox/grant_v_views.sql << 'GRANT_SQL_EOF'
    ALTER SESSION SET CONTAINER = FREEPDB1;
    GRANT SELECT ON V_$SESSION TO executor_sa;
    GRANT SELECT ON V_$SYSMETRIC TO executor_sa;
    GRANT SELECT ON V_$INSTANCE TO executor_sa;
    GRANT SELECT ON V_$VERSION TO executor_sa;
    GRANT ALTER SYSTEM TO executor_sa;
    EXIT;
    GRANT_SQL_EOF

    export ORACLE_PW="${var.oracle_db_password}"
    nohup bash -c '
      for i in $(seq 1 60); do
        docker exec -i oracle-db sqlplus -s "sys/$ORACLE_PW@127.0.0.1:1521/FREEPDB1" as sysdba < /var/lib/toolbox/grant_v_views.sql > /tmp/grant_attempt.log 2>&1
        if ! grep -qi "ORA-\|SP2-" /tmp/grant_attempt.log; then
          cat /tmp/grant_attempt.log
          exit 0
        fi
        sleep 10
      done
      echo "grant script exhausted retries -- last attempt output:"
      cat /tmp/grant_attempt.log
    ' > /var/log/grant_v_views.log 2>&1 &
  EOT

  depends_on = [google_project_service.apis]
}
