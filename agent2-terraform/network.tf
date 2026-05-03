############################################
# Resource group
############################################

resource "azurerm_resource_group" "this" {
  count    = var.create_resource_group ? 1 : 0
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

data "azurerm_resource_group" "existing" {
  count = var.create_resource_group ? 0 : 1
  name  = var.resource_group_name
}

locals {
  rg_name     = var.create_resource_group ? azurerm_resource_group.this[0].name : data.azurerm_resource_group.existing[0].name
  rg_location = var.create_resource_group ? azurerm_resource_group.this[0].location : data.azurerm_resource_group.existing[0].location

  base_tags = {
    project     = var.project
    environment = var.environment
    managed_by  = "terraform"
    workload    = "oracle-exadata-odaa"
  }
  tags = merge(local.base_tags, var.tags)
}

############################################
# VNet + subnets (created when create_network = true)
############################################

resource "azurerm_virtual_network" "this" {
  count               = var.create_network ? 1 : 0
  name                = var.vnet_name
  location            = local.rg_location
  resource_group_name = local.rg_name
  address_space       = var.vnet_address_space
  tags                = local.tags
}

# Client subnet — MUST be delegated to Oracle.Database/networkAttachments.
# Oracle attaches the VM cluster vNICs into this subnet.
resource "azurerm_subnet" "client" {
  count                = var.create_network ? 1 : 0
  name                 = var.client_subnet_name
  resource_group_name  = local.rg_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.client_subnet_cidr]

  delegation {
    name = "oracle-db-delegation"
    service_delegation {
      name = "Oracle.Database/networkAttachments"
      actions = [
        "Microsoft.Network/networkinterfaces/*",
        "Microsoft.Network/virtualNetworks/subnets/join/action",
      ]
    }
  }
}

resource "azurerm_subnet" "backup" {
  count                = var.create_network ? 1 : 0
  name                 = var.backup_subnet_name
  resource_group_name  = local.rg_name
  virtual_network_name = azurerm_virtual_network.this[0].name
  address_prefixes     = [var.backup_subnet_cidr]
}

############################################
# Resolve effective IDs (created or existing)
############################################

locals {
  vnet_id          = var.create_network ? azurerm_virtual_network.this[0].id : var.existing_vnet_id
  client_subnet_id = var.create_network ? azurerm_subnet.client[0].id : var.existing_client_subnet_id
  backup_subnet_id = var.create_network ? azurerm_subnet.backup[0].id : var.existing_backup_subnet_id
}

############################################
# NSG (optional) — restricts SCAN access
############################################

resource "azurerm_network_security_group" "client" {
  count               = length(var.nsg_allowed_source_cidrs) > 0 ? 1 : 0
  name                = "nsg-${var.project}-${var.environment}-odaa-client"
  location            = local.rg_location
  resource_group_name = local.rg_name
  tags                = local.tags
}

resource "azurerm_network_security_rule" "scan_tcp" {
  count                       = length(var.nsg_allowed_source_cidrs) > 0 ? 1 : 0
  name                        = "Allow-SCAN-TCP"
  priority                    = 200
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = tostring(var.scan_listener_port_tcp)
  source_address_prefixes     = var.nsg_allowed_source_cidrs
  destination_address_prefix  = var.client_subnet_cidr
  resource_group_name         = local.rg_name
  network_security_group_name = azurerm_network_security_group.client[0].name
}

resource "azurerm_network_security_rule" "scan_tcps" {
  count                       = length(var.nsg_allowed_source_cidrs) > 0 ? 1 : 0
  name                        = "Allow-SCAN-TCPS"
  priority                    = 210
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_range      = tostring(var.scan_listener_port_tcp_ssl)
  source_address_prefixes     = var.nsg_allowed_source_cidrs
  destination_address_prefix  = var.client_subnet_cidr
  resource_group_name         = local.rg_name
  network_security_group_name = azurerm_network_security_group.client[0].name
}

resource "azurerm_subnet_network_security_group_association" "client" {
  count                     = (var.create_network && length(var.nsg_allowed_source_cidrs) > 0) ? 1 : 0
  subnet_id                 = azurerm_subnet.client[0].id
  network_security_group_id = azurerm_network_security_group.client[0].id
}
