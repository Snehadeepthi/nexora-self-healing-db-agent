# MCP Toolbox for Databases used to run as its own Cloud Run v2 service here.
# It was moved onto the Oracle VM itself as a plain sibling container --
# see database.tf's metadata_startup_script for the actual resource, and
# cloudrun.tf's TOOLBOX_URL/TOOLBOX_REQUIRE_AUTH env vars for how the
# orchestrator now reaches it.
#
# Why: Toolbox-on-Cloud-Run, reaching this VM's Oracle listener through
# either the Serverless VPC Access connector or Direct VPC Egress, was
# empirically confirmed to corrupt Oracle's binary O5LOGON auth handshake in
# transit -- every attempt failed with a generic ORA-01017 "invalid
# username/password" that had nothing to do with the actual credentials
# (verified correct multiple times over, including by connecting with the
# exact same official Toolbox image and the exact same driver library
# directly on the VM, which worked immediately). Running Toolbox next to
# Oracle instead means that connection is 127.0.0.1, and the only thing left
# to cross the connector is Toolbox's own plain HTTP API -- a far more
# tolerant protocol, and the same general shape of traffic already working
# reliably elsewhere in this deployment (Cloud Scheduler -> orchestrator).
