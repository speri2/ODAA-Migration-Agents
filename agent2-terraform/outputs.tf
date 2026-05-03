output "resource_group_name" {
  value = local.rg_name
}

output "location" {
  value = local.rg_location
}

output "exadata_infrastructure_id" {
  description = "Azure resource ID of the Exadata Infrastructure."
  value       = azurerm_oracle_exadata_infrastructure.this.id
}

output "exadata_infrastructure_ocid" {
  description = "Oracle Cloud OCID for the Exadata Infrastructure."
  value       = azurerm_oracle_exadata_infrastructure.this.ocid
}

output "vm_cluster_id" {
  value = azurerm_oracle_cloud_vm_cluster.this.id
}

output "vm_cluster_ocid" {
  value = azurerm_oracle_cloud_vm_cluster.this.ocid
}

output "scan_dns_name" {
  description = "SCAN DNS hostname — use this in tnsnames.ora for the migration target."
  value       = azurerm_oracle_cloud_vm_cluster.this.scan_dns_name
}

output "scan_listener_port_tcp" {
  value = var.scan_listener_port_tcp
}

output "scan_listener_port_tcp_ssl" {
  value = var.scan_listener_port_tcp_ssl
}

output "scan_ip_ids" {
  value = azurerm_oracle_cloud_vm_cluster.this.scan_ip_ids
}

output "vip_ip_ids" {
  value = azurerm_oracle_cloud_vm_cluster.this.vip_ods
}

output "client_subnet_id" {
  value = local.client_subnet_id
}

output "vnet_id" {
  value = local.vnet_id
}

output "ssh_private_key_path" {
  description = "Local path of the generated private key (null if user-supplied keys were used)."
  value       = length(var.ssh_public_keys) == 0 ? var.ssh_key_output_path : null
}

############################################
# JSON contract for Agent 3 (Oracle Net validation)
############################################

locals {
  agent3_handoff = {
    schema_version = "1.0"
    generated_at   = timestamp()
    agent          = "agent2-terraform"
    next_agent     = "agent3-oranet-validation"

    azure = {
      subscription_id     = data.azurerm_client_config.current.subscription_id
      tenant_id           = data.azurerm_client_config.current.tenant_id
      resource_group_name = local.rg_name
      location            = local.rg_location
      availability_zone   = var.availability_zone
    }

    exadata_infrastructure = {
      azure_id      = azurerm_oracle_exadata_infrastructure.this.id
      ocid          = azurerm_oracle_exadata_infrastructure.this.ocid
      shape         = var.exa_shape
      compute_count = var.compute_count
      storage_count = var.storage_count
    }

    vm_cluster = {
      azure_id          = azurerm_oracle_cloud_vm_cluster.this.id
      ocid              = azurerm_oracle_cloud_vm_cluster.this.ocid
      cluster_name      = var.cluster_name
      hostname_prefix   = var.hostname_prefix
      gi_version        = var.gi_version
      license_model     = var.license_model
      cpu_core_count    = var.cpu_core_count
      memory_gb         = var.memory_size_in_gbs
      data_storage_tb   = var.data_storage_size_in_tbs
      domain            = var.domain
      time_zone         = var.time_zone
    }

    network = {
      vnet_id           = local.vnet_id
      client_subnet_id  = local.client_subnet_id
      backup_subnet_id  = local.backup_subnet_id
      client_subnet_cidr = var.client_subnet_cidr
    }

    oracle_net = {
      scan_dns_name              = azurerm_oracle_cloud_vm_cluster.this.scan_dns_name
      scan_listener_port_tcp     = var.scan_listener_port_tcp
      scan_listener_port_tcp_ssl = var.scan_listener_port_tcp_ssl
    }

    ssh = {
      private_key_path  = length(var.ssh_public_keys) == 0 ? var.ssh_key_output_path : null
      authorized_keys   = local.effective_ssh_keys
      os_user           = "opc"
    }
  }
}

data "azurerm_client_config" "current" {}

resource "local_file" "agent3_handoff" {
  filename        = "${path.module}/agent3_input.json"
  content         = jsonencode(local.agent3_handoff)
  file_permission = "0640"
}

output "agent3_handoff_path" {
  description = "Path to the JSON handoff document Agent 3 (Oracle Net validation) must consume."
  value       = local_file.agent3_handoff.filename
}

output "agent3_handoff" {
  description = "Inline JSON handoff for Agent 3."
  value       = local.agent3_handoff
  sensitive   = false
}
