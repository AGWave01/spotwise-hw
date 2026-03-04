# Azure Deployment Plan — Budget Edition

> Goal: run N radio recorders 24/7 in Azure at minimum cost.  
> Philosophy: replace expensive managed Azure services with self-hosted open-source tools where the operational overhead is acceptable.

---

## 1. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          AZURE SUBSCRIPTION                                  │
│                                                                              │
│   ┌────────────────────────────────────────────────────────────────────┐     │
│   │            Azure VM  (Standard_B2s — 2 vCPU / 4 GB RAM)            │     │
│   │                     radio-recorder-vm                              │     │
│   │                                                                    │     │
│   │   Docker Engine (docker compose)                                   │     │
│   │                                                                    │     │
│   │  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                │     │
│   │  │  recorder-   │ │  recorder-   │ │  recorder-   │                │     │
│   │  │    dsr       │ │   ehrhiti    │ │  station-N   │                │     │
│   │  └──────┬───────┘ └──────┬───────┘ └──────┬───────┘                │     │
│   │         └────────────────┴────────────────┘                        │     │
│   │                          │ write MP3                               │     │
│   │                          ▼                                         │     │
│   │             /mnt/recordings  (mounted disk)                        │     │
│   │                          │                                         │     │
│   │   ┌───────────────────────────────────────────────────────┐        │     │
│   │   │  Sidecar containers (same compose stack)              │        │     │
│   │   │                                                       │        │     │
│   │   │  ┌──────────────────┐   ┌──────────────────────────┐  │        │     │
│   │   │  │  upload-agent    │   │  HashiCorp Vault (dev)   │  │        │     │
│   │   │  │  (rclone daemon) │   │  :8200                   │  │        │     │
│   │   │  │  watches dir,    │   │  stores: storage keys,   │  │        │     │
│   │   │  │  uploads to Blob │   │  stream credentials      │  │        │     │
│   │   │  └──────────────────┘   └──────────────────────────┘  │        │     │
│   │   │                                                       │        │     │
│   │   │  ┌──────────────────┐   ┌──────────────────────────┐  │        │     │
│   │   │  │  VictoriaMetrics │   │  Grafana :3000           │  │        │     │
│   │   │  │  :8428           │   │  dashboards + alerts     │  │        │     │
│   │   │  │  metrics store   │   │  → email/Telegram        │  │        │     │
│   │   │  └──────────────────┘   └──────────────────────────┘  │        │     │
│   │   └───────────────────────────────────────────────────────┘        │     │
│   └────────────────────────────────────────────────────────────────────┘     │
│                          │ rclone sync                                       │
│                          ▼                                                   │
│        ┌─────────────────────────────────────┐                               │
│        │      Azure Blob Storage             │                               │
│        │   (radiorecordingssa — LRS)         │                               │
│        │   Container: recordings             │                               │
│        │   ├── dsr/                          │                               │
│        │   └── ehrhiti/                      │                               │
│        └─────────────────────────────────────┘                               │
│                          │ poll (rclone / custom script)                     │
│                    downstream consumers                                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Resource Selection

### Compute — Azure VM `Standard_B2s`

- **~$30/month** (1-year reserved) or ~$35/month pay-as-you-go
- 2 vCPU / 4 GB — handles 5–8 parallel ffmpeg processes easily
- All containers run via `docker compose` on the VM — no container orchestration needed at this scale
- OS disk: Standard SSD 32 GB (~$2/month)

> For 20 stations: upgrade to `Standard_B4ms` (4 vCPU / 16 GB, ~$60/month reserved) — still cheaper than 20 × Container Apps.

### Storage — Azure Blob Storage (LRS, Cool tier)

- **~$0.01/GB/month** (Cool tier) vs $0.018/GB (Hot)
- Upload via **rclone** sidecar — dead simple, battle-tested, single binary
- No Event Grid needed — downstream consumers poll with rclone or a cron script
- LRS (Locally Redundant) instead of ZRS/GRS — saves ~30% on storage cost

### Secrets — HashiCorp Vault (self-hosted, Dev/file backend)

- Free, runs as a Docker container on the same VM
- Stores: Azure Storage connection string, stream credentials
- Recorder reads secrets at startup via Vault HTTP API or env injection script
- **Tradeoff**: self-hosted Vault. For a small team this is fine; for production at scale, use Azure Key Vault.

```yaml
vault:
  image: hashicorp/vault:1.15
  cap_add: [IPC_LOCK]
  environment:
    VAULT_DEV_ROOT_TOKEN_ID: "dev-token"
    VAULT_DEV_LISTEN_ADDRESS: "0.0.0.0:8200"
  ports:
    - "127.0.0.1:8200:8200"
```

```bash
vault kv put secret/recorder \
  azure_storage_conn="DefaultEndpointsProtocol=https;..."
```

### Monitoring — VictoriaMetrics + Grafana

- **Free** (open source), both run as Docker containers
- VictoriaMetrics: Prometheus-compatible metrics store, uses ~50 MB RAM at this scale
- Grafana: dashboards + alert rules → email or Telegram bot (free)
- Metrics to track:
  - `recorder_files_written_total{station="dsr"}` — custom counter via a small bash script that counts MP3s per minute
  - `node_filesystem_avail_bytes` — disk space (via node_exporter)
  - `container_cpu_usage_seconds_total` — ffmpeg CPU

```yaml
victoriametrics:
  image: victoriametrics/victoria-metrics:latest
  ports: ["127.0.0.1:8428:8428"]
  volumes: ["vm-data:/storage"]
  command: ["-storageDataPath=/storage", "-retentionPeriod=30d"]

grafana:
  image: grafana/grafana:latest
  ports: ["0.0.0.0:3000:3000"]
  volumes: ["grafana-data:/var/lib/grafana"]
```

### Notifications — simple bash + cron

No Event Grid, no Service Bus. A cron job on the VM checks for new MP3 files and POSTs to a webhook (Slack, Teams, custom endpoint):

```bash
#!/bin/bash
NEW=$(find /mnt/recordings -name "*.mp3" -newer /tmp/last_check -type f)
if [ -n "$NEW" ]; then
  echo "$NEW" | while read f; do
    curl -s -X POST "$WEBHOOK_URL" \
      -H "Content-Type: application/json" \
      -d "{\"text\": \"New recording: $(basename $f)\"}"
  done
fi
touch /tmp/last_check
```

---

## 3. Justification

| Component     | Budget choice                 | Azure native alternative      | Why budget wins here                                                                                      |
| ------------- | ----------------------------- | ----------------------------- | --------------------------------------------------------------------------------------------------------- |
| Compute       | **VM B2s**                    | Container Apps                | $30/month flat vs ~$35+ per-app billing. Simple docker compose, no ACA learning curve.                    |
| Secrets       | **HashiCorp Vault**           | Azure Key Vault               | Free. For a small team running a handful of services, self-hosted Vault is perfectly reasonable.          |
| Monitoring    | **VictoriaMetrics + Grafana** | Azure Monitor + Log Analytics | Log Analytics charges $2.30/GB ingested — at INFO level this adds $300+/month. VM+containers = flat cost. |
| Notifications | **cron + webhook**            | Event Grid + Service Bus      | At <100 events/day the entire SB/EG stack is overkill. A 5-line bash script does the job.                 |
| Storage       | **Blob Cool tier + rclone**   | Blob Hot + Event Grid         | Cool tier is 44% cheaper. rclone is a single binary with zero Azure-specific lock-in.                     |

---

## 4. Scaling — 1 → 20 Stations

Add a station = add one service block to `docker-compose.yml`.

For 20 stations on one VM, use `Standard_B4ms` (4 vCPU / 16 GB). Each ffmpeg process uses ~0.1–0.15 vCPU at steady state — 20 stations = ~3 vCPU peak.

### Terraform — provision the VM

```hcl
# main.tf
terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.0" }
  }
}

provider "azurerm" { features {} }

variable "location"      { default = "westeurope" }
variable "vm_size"       { default = "Standard_B2s" }
variable "admin_username" { default = "azureuser" }
variable "ssh_public_key" {}

resource "azurerm_resource_group" "rg" {
  name     = "radio-recorder-rg"
  location = var.location
}

resource "azurerm_virtual_network" "vnet" {
  name                = "radio-vnet"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  address_space       = ["10.0.0.0/16"]
}

resource "azurerm_subnet" "subnet" {
  name                 = "radio-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_public_ip" "pip" {
  name                = "radio-pip"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Basic"
}

resource "azurerm_network_security_group" "nsg" {
  name                = "radio-nsg"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location

  security_rule {
    name                       = "SSH"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "22"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_interface" "nic" {
  name                = "radio-nic"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
    public_ip_address_id          = azurerm_public_ip.pip.id
  }
}

resource "azurerm_network_interface_security_group_association" "nsg_assoc" {
  network_interface_id      = azurerm_network_interface.nic.id
  network_security_group_id = azurerm_network_security_group.nsg.id
}

resource "azurerm_linux_virtual_machine" "vm" {
  name                = "radio-recorder-vm"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.location
  size                = var.vm_size
  admin_username      = var.admin_username

  network_interface_ids = [azurerm_network_interface.nic.id]

  admin_ssh_key {
    username   = var.admin_username
    public_key = var.ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
    disk_size_gb         = 32
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }

  # Bootstrap: install Docker on first boot
  custom_data = base64encode(<<-EOF
    #!/bin/bash
    apt-get update
    apt-get install -y docker.io docker-compose-plugin
    systemctl enable --now docker
    usermod -aG docker ${var.admin_username}
    mkdir -p /mnt/recordings
    EOF
  )
}

resource "azurerm_storage_account" "sa" {
  name                     = "radiorecordingssa"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  access_tier              = "Cool"
}

resource "azurerm_storage_container" "recordings" {
  name                  = "recordings"
  storage_account_name  = azurerm_storage_account.sa.name
  container_access_type = "private"
}

output "vm_public_ip" {
  value = azurerm_public_ip.pip.ip_address
}

output "storage_connection_string" {
  value     = azurerm_storage_account.sa.primary_connection_string
  sensitive = true
}
```

**Deploy:**
```bash
terraform init
terraform plan -var="ssh_public_key=$(cat ~/.ssh/id_rsa.pub)"
terraform apply -var="ssh_public_key=$(cat ~/.ssh/id_rsa.pub)"

terraform output -raw storage_connection_string
```

**Scale to 20 stations:** change `vm_size = "Standard_B4ms"` + `terraform apply`. Done.

---

## 5. Deployment Steps

### Option A — Azure CLI

```bash
az group create --name radio-recorder-rg --location westeurope

az storage account create \
  --name radiorecordingssa \
  --resource-group radio-recorder-rg \
  --sku Standard_LRS --kind StorageV2 \
  --access-tier Cool

az storage container create \
  --name recordings \
  --account-name radiorecordingssa

az vm create \
  --resource-group radio-recorder-rg \
  --name radio-recorder-vm \
  --image Ubuntu2204 \
  --size Standard_B2s \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/id_rsa.pub \
  --custom-data cloud-init.yml

VM_IP=$(az vm show -d -g radio-recorder-rg -n radio-recorder-vm --query publicIps -o tsv)

ssh azureuser@$VM_IP
git clone https://github.com/<smth>/radio-recorder.git
cd radio-recorder
AZURE_STORAGE_CONN=$(az storage account show-connection-string \
  --name radiorecordingssa --resource-group radio-recorder-rg -o tsv)

docker compose up -d
```

### Option B — Terraform

```bash
git clone https://github.com/<smth>/radio-recorder.git
cd radio-recorder/terraform

terraform init
terraform apply -var="ssh_public_key=$(cat ~/.ssh/id_rsa.pub)"

ssh azureuser@$(terraform output -raw vm_public_ip)

git clone https://github.com/<smth>/radio-recorder.git && cd radio-recorder
docker compose up -d
```

---

## Cost Summary (20 stations)

| Component                         | Monthly cost   |
| --------------------------------- | -------------- |
| VM Standard_B4ms (reserved 1yr)   | ~$60           |
| Blob Storage Cool ~900 GB         | ~$9            |
| Public IP                         | ~$4            |
| OS disk Standard SSD              | ~$2            |
| VictoriaMetrics + Grafana + Vault | $0 (on VM)     |
| Event Grid / Service Bus          | $0 (not used)  |
| Log Analytics                     | $0 (not used)  |
| **Total**                         | **~$75/month** |

> Compare to managed-services plan: ~$85–90/month. Saves ~$15/month but trades managed services for self-hosted ops.  
> Real saving is in **Log Analytics avoidance** (~$300/month).