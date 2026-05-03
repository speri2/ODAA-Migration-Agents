############################################
# SSH key (generated when none supplied)
############################################

resource "tls_private_key" "generated" {
  count     = length(var.ssh_public_keys) == 0 ? 1 : 0
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "local_sensitive_file" "private_key" {
  count           = length(var.ssh_public_keys) == 0 ? 1 : 0
  filename        = var.ssh_key_output_path
  content         = tls_private_key.generated[0].private_key_openssh
  file_permission = "0600"
}

locals {
  effective_ssh_keys = length(var.ssh_public_keys) > 0 ? var.ssh_public_keys : [tls_private_key.generated[0].public_key_openssh]
}

############################################
# Exadata Infrastructure
# (the physical Exadata system Oracle provisions in the AZ)
############################################

resource "azurerm_oracle_exadata_infrastructure" "this" {
  name                = "exa-${var.project}-${var.environment}"
  resource_group_name = local.rg_name
  location            = local.rg_location
  zones               = [var.availability_zone]

  display_name      = var.exa_infra_display_name
  shape             = var.exa_shape
  compute_count     = var.compute_count
  storage_count     = var.storage_count
  customer_contacts = var.customer_contacts

  maintenance_window {
    preference         = var.maintenance_window.preference
    patching_mode      = var.maintenance_window.patching_mode
    months             = var.maintenance_window.months
    weeks_of_month     = var.maintenance_window.weeks_of_month
    days_of_week       = var.maintenance_window.days_of_week
    hours_of_day       = var.maintenance_window.hours_of_day
    lead_time_in_weeks = var.maintenance_window.lead_time_in_weeks
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.compute_count >= 2 && var.storage_count >= 3
      error_message = "Production ODAA requires compute_count >= 2 and storage_count >= 3."
    }
    precondition {
      condition     = var.data_storage_size_in_tbs <= var.storage_count * 63
      error_message = "Requested data_storage_size_in_tbs exceeds capacity of storage_count cells (~63 TB usable per X9M cell). Increase storage_count."
    }
  }
}

############################################
# DB servers (data source — needed for VM cluster)
############################################

data "azurerm_oracle_db_servers" "this" {
  resource_group_name              = local.rg_name
  cloud_exadata_infrastructure_name = azurerm_oracle_exadata_infrastructure.this.name
}

locals {
  db_server_ocids = [for s in data.azurerm_oracle_db_servers.this.db_servers : s.ocid]
}

############################################
# Cloud VM Cluster
############################################

resource "azurerm_oracle_cloud_vm_cluster" "this" {
  name                = "vmc-${var.project}-${var.environment}"
  resource_group_name = local.rg_name
  location            = local.rg_location
  zones               = [var.availability_zone]

  cloud_exadata_infrastructure_id = azurerm_oracle_exadata_infrastructure.this.id
  display_name                    = var.vmcluster_display_name
  cluster_name                    = var.cluster_name
  hostname                        = var.hostname_prefix
  gi_version                      = var.gi_version
  license_model                   = var.license_model
  time_zone                       = var.time_zone

  cpu_core_count              = var.cpu_core_count
  memory_size_in_gbs          = var.memory_size_in_gbs
  db_node_storage_size_in_gbs = var.db_node_storage_size_in_gbs
  data_storage_size_in_tbs    = var.data_storage_size_in_tbs
  data_storage_percentage     = var.data_storage_percentage

  is_local_backup_enabled     = var.is_local_backup_enabled
  is_sparse_diskgroup_enabled = var.is_sparse_diskgroup_enabled

  ssh_public_keys = local.effective_ssh_keys
  db_servers      = local.db_server_ocids

  subnet_id = local.client_subnet_id
  vnet_id   = local.vnet_id

  scan_listener_port_tcp     = var.scan_listener_port_tcp
  scan_listener_port_tcp_ssl = var.scan_listener_port_tcp_ssl
  domain                     = var.domain
  backup_subnet_cidr         = var.backup_subnet_cidr

  data_collection_options {
    diagnostics_events_enabled = var.data_collection_options.diagnostics_events_enabled
    health_monitoring_enabled  = var.data_collection_options.health_monitoring_enabled
    incident_logs_enabled      = var.data_collection_options.incident_logs_enabled
  }

  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.cpu_core_count % var.compute_count == 0
      error_message = "cpu_core_count must distribute evenly across compute_count nodes."
    }
    precondition {
      condition     = length(local.db_server_ocids) >= var.compute_count
      error_message = "DB server discovery did not return enough servers — check provider/RP registration."
    }
  }

  depends_on = [
    azurerm_oracle_exadata_infrastructure.this,
  ]
}
