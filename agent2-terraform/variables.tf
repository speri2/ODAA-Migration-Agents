############################################
# Identity / placement
############################################

variable "project" {
  description = "Short project tag, used in resource names. lowercase, 3-10 chars."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9]{2,9}$", var.project))
    error_message = "project must be 3-10 lowercase alphanumeric characters starting with a letter."
  }
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  validation {
    condition     = contains(["dev", "test", "stage", "prod"], var.environment)
    error_message = "environment must be one of: dev, test, stage, prod."
  }
}

variable "location" {
  description = "Azure region that supports Oracle Database@Azure (e.g. eastus, germanywestcentral, ukso, australiaeast)."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group to create / use for ODAA resources."
  type        = string
}

variable "create_resource_group" {
  description = "Create the resource group (true) or expect it to exist (false)."
  type        = bool
  default     = true
}

variable "availability_zone" {
  description = "Availability zone for the Exadata Infrastructure (string '1', '2', or '3'). Must be an Oracle-enabled zone in the chosen region."
  type        = string
  validation {
    condition     = contains(["1", "2", "3"], var.availability_zone)
    error_message = "availability_zone must be '1', '2', or '3'."
  }
}

############################################
# Exadata Infrastructure
############################################

variable "exa_infra_display_name" {
  description = "Display name of the Exadata Infrastructure shown in OCI/Azure portals."
  type        = string
}

variable "exa_shape" {
  description = "Exadata system shape. X9M is the most common GA shape on ODAA; X11M where available."
  type        = string
  default     = "Exadata.X9M"
  validation {
    condition     = contains(["Exadata.X9M", "Exadata.X11M"], var.exa_shape)
    error_message = "exa_shape must be Exadata.X9M or Exadata.X11M."
  }
}

variable "compute_count" {
  description = "Number of DB servers in the infrastructure (>= 2). For 100 TB usable on X9M, 2 is sufficient; scale up for HA / OLTP throughput."
  type        = number
  default     = 2
  validation {
    condition     = var.compute_count >= 2 && var.compute_count <= 32
    error_message = "compute_count must be between 2 and 32."
  }
}

variable "storage_count" {
  description = "Number of storage cells. Each X9M cell delivers ~63.6 TB usable (NORMAL redundancy). For 100 TB usable: minimum 3."
  type        = number
  default     = 3
  validation {
    condition     = var.storage_count >= 3 && var.storage_count <= 64
    error_message = "storage_count must be between 3 and 64."
  }
}

variable "customer_contacts" {
  description = "List of email addresses Oracle will notify for hardware events. Required for production."
  type        = list(string)
  validation {
    condition     = length(var.customer_contacts) >= 1
    error_message = "At least one customer contact email is required."
  }
}

variable "maintenance_window" {
  description = "Maintenance window for Oracle-driven patching of the infrastructure."
  type = object({
    preference         = string                # "NoPreference" or "CustomPreference"
    patching_mode      = optional(string, "Rolling")
    months             = optional(list(string), [])
    weeks_of_month     = optional(list(number), [])
    days_of_week       = optional(list(string), [])
    hours_of_day       = optional(list(number), [])
    lead_time_in_weeks = optional(number, 2)
  })
  default = {
    preference         = "CustomPreference"
    patching_mode      = "Rolling"
    months             = ["January", "April", "July", "October"]
    weeks_of_month     = [2]
    days_of_week       = ["Sunday"]
    hours_of_day       = [4]
    lead_time_in_weeks = 2
  }
}

############################################
# VM Cluster
############################################

variable "vmcluster_display_name" {
  description = "Display name of the VM Cluster."
  type        = string
}

variable "cluster_name" {
  description = "GI cluster name (<= 11 chars, alphanumeric + hyphen)."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,10}$", var.cluster_name))
    error_message = "cluster_name must be <=11 chars, lowercase alphanumeric + hyphen, starting with a letter."
  }
}

variable "hostname_prefix" {
  description = "Hostname prefix for VM nodes (<= 12 chars). Oracle suffixes node numbers."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,11}$", var.hostname_prefix))
    error_message = "hostname_prefix must be <=12 chars, lowercase alphanumeric + hyphen."
  }
}

variable "gi_version" {
  description = "Oracle Grid Infrastructure major version."
  type        = string
  default     = "19.0.0.0"
}

variable "license_model" {
  description = "Oracle licensing posture."
  type        = string
  default     = "BringYourOwnLicense"
  validation {
    condition     = contains(["LicenseIncluded", "BringYourOwnLicense"], var.license_model)
    error_message = "license_model must be LicenseIncluded or BringYourOwnLicense."
  }
}

variable "cpu_core_count" {
  description = "Enabled OCPUs across the cluster. Multiple of compute_count. Min 2 per node on X9M."
  type        = number
  default     = 16
  validation {
    condition     = var.cpu_core_count >= 4 && var.cpu_core_count <= 760
    error_message = "cpu_core_count must be between 4 and 760."
  }
}

variable "memory_size_in_gbs" {
  description = "Cluster memory in GB. X9M supports up to 1390 GB per node."
  type        = number
  default     = 360
  validation {
    condition     = var.memory_size_in_gbs >= 60 && var.memory_size_in_gbs <= 44480
    error_message = "memory_size_in_gbs out of supported range."
  }
}

variable "db_node_storage_size_in_gbs" {
  description = "Local /u02 storage per node (GB)."
  type        = number
  default     = 500
}

variable "data_storage_size_in_tbs" {
  description = "Usable DATA storage in TB. Sized to 100 by default."
  type        = number
  default     = 100
  validation {
    condition     = var.data_storage_size_in_tbs >= 2 && var.data_storage_size_in_tbs <= 192
    error_message = "data_storage_size_in_tbs must be between 2 and 192."
  }
}

variable "data_storage_percentage" {
  description = "Percent of total ASM storage allocated to DATA disk group (rest goes to RECO)."
  type        = number
  default     = 80
  validation {
    condition     = contains([35, 40, 60, 80], var.data_storage_percentage)
    error_message = "data_storage_percentage must be one of 35, 40, 60, 80."
  }
}

variable "is_local_backup_enabled" {
  description = "Provision a RECO disk group on Exadata cells (local backup). Recommended false when using ZRCV or Azure Blob."
  type        = bool
  default     = false
}

variable "is_sparse_diskgroup_enabled" {
  description = "Create a SPARSE disk group for clones. Reduces DATA capacity."
  type        = bool
  default     = false
}

variable "time_zone" {
  description = "OS time zone for the cluster nodes."
  type        = string
  default     = "UTC"
}

variable "ssh_public_keys" {
  description = "SSH public keys for opc user. If empty, a key pair is generated and the private key written to ssh_key_output_path."
  type        = list(string)
  default     = []
}

variable "ssh_key_output_path" {
  description = "Path to write a generated private key to (only used when ssh_public_keys is empty)."
  type        = string
  default     = "./generated_id_rsa"
}

variable "data_collection_options" {
  description = "Diagnostic / health monitoring opt-ins forwarded to Oracle."
  type = object({
    diagnostics_events_enabled = optional(bool, true)
    health_monitoring_enabled  = optional(bool, true)
    incident_logs_enabled      = optional(bool, true)
  })
  default = {}
}

variable "domain" {
  description = "Optional DNS domain for the cluster (must resolve in the Azure VNet)."
  type        = string
  default     = null
}

variable "scan_listener_port_tcp" {
  description = "SCAN listener TCP port."
  type        = number
  default     = 1521
}

variable "scan_listener_port_tcp_ssl" {
  description = "SCAN listener TCPS port."
  type        = number
  default     = 2484
}

############################################
# Networking
############################################

variable "create_network" {
  description = "Create the VNet/subnets (true) or use existing IDs (false)."
  type        = bool
  default     = true
}

variable "vnet_name" {
  description = "VNet name (created or existing)."
  type        = string
}

variable "vnet_address_space" {
  description = "Address space for a newly-created VNet."
  type        = list(string)
  default     = ["10.50.0.0/16"]
}

variable "client_subnet_name" {
  description = "Client subnet (delegated to Oracle.Database/networkAttachments)."
  type        = string
  default     = "snet-odaa-client"
}

variable "client_subnet_cidr" {
  description = "Client subnet CIDR. Min /24, must not overlap other VNets/peers."
  type        = string
  default     = "10.50.10.0/24"
}

variable "backup_subnet_name" {
  description = "Optional backup subnet."
  type        = string
  default     = "snet-odaa-backup"
}

variable "backup_subnet_cidr" {
  description = "Backup subnet CIDR (used by VM cluster for backup traffic)."
  type        = string
  default     = "10.50.11.0/24"
}

variable "existing_vnet_id" {
  description = "Existing VNet resource ID (used when create_network = false)."
  type        = string
  default     = null
}

variable "existing_client_subnet_id" {
  description = "Existing client subnet ID (used when create_network = false). Must already be delegated to Oracle.Database/networkAttachments."
  type        = string
  default     = null
}

variable "existing_backup_subnet_id" {
  description = "Existing backup subnet ID."
  type        = string
  default     = null
}

variable "nsg_allowed_source_cidrs" {
  description = "Source CIDRs allowed to reach SCAN listener ports. Empty = no NSG rules added."
  type        = list(string)
  default     = []
}

############################################
# Tagging
############################################

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
