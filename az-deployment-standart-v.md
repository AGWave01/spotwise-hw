# Azure Deployment Plan — Standard Edition

> Goal: run N radio recorders 24/7 in Azure using managed services.  
> Philosophy: minimize operational overhead; let Azure handle availability, secrets rotation, log aggregation, and event routing.

---

## 1. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              AZURE SUBSCRIPTION                                 │
│                                                                                 │
│   ┌───────────────────────────────────────────────────────────────────────┐     │
│   │              Azure Container Apps Environment                         │     │
│   │                  (radio-recorder-env)                                 │     │
│   │                                                                       │     │
│   │  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────────┐   │     │
│   │  │  Container App   │ │  Container App   │ │    Container App     │   │     │
│   │  │  recorder-dsr    │ │ recorder-ehrhiti │ │   recorder-station-N │   │     │
│   │  │                  │ │                  │ │                      │   │     │
│   │  │ min-replicas: 1  │ │ min-replicas: 1  │ │  min-replicas: 1     │   │     │
│   │  │ 0.25vCPU/0.5GB   │ │ 0.25vCPU/0.5GB   │ │  0.25vCPU/0.5GB      │   │     │
│   │  └────────┬─────────┘ └────────┬─────────┘ └──────────┬───────────┘   │     │
│   │           └──────────────────┬─┘                      │               │     │
│   └──────────────────────────────┼──────────────────────────┘             │     │
│                                  │ upload MP3 via azure-storage-blob SDK  │     │
│                                  ▼                                        │     │
│            ┌──────────────────────────────────────────┐                   │     │
│            │         Azure Blob Storage               │                   │     │
│            │      (radiorecordingssa — LRS)           │                   │     │
│            │                                          │                   │     │
│            │  Container: recordings                   │                   │     │
│            │  ├── dsr/dsr_20260304_190000_0000.mp3    │                   │     │
│            │  └── ehrhiti/...mp3                      │                   │     │
│            │                                          │                   │     │
│            │  Lifecycle: Hot→Cool (7d)→Archive (30d)  │                   │     │
│            └─────────────────┬────────────────────────┘                   │     │
│                              │ Microsoft.Storage.BlobCreated              │     │
│                              ▼                                            │     │
│            ┌──────────────────────────────────────────┐                   │     │
│            │          Azure Event Grid                │                   │     │
│            │      (radio-recordings-evgt)             │                   │     │
│            │  Filter: subject endsWith ".mp3"         │                   │     │
│            └─────────────────┬────────────────────────┘                   │     │
│                              │                                            │     │
│            ┌─────────────────▼────────────────────────┐                   │     │
│            │       Azure Service Bus Queue            │                   │     │
│            │   (radio-sb) / Queue: new-recordings     │                   │     │
│            │   Dead-letter queue included             │                   │     │
│            └─────────────────┬────────────────────────┘                   │     │
│                              │                                            │     │
│                    downstream consumers                                   │     │
│             (transcription, archival, playback API, etc.)                 │     │
│                                                                           │     │
│   ┌───────────────────────────────────────────────────────────────────┐   │     │
│   │                  Monitoring & Observability                       │   │     │
│   │                                                                   │   │     │
│   │  Container Apps ──stdout/stderr──► Log Analytics Workspace        │   │     │
│   │                                   (radio-logs-law)                │   │     │
│   │                                        │                          │   │     │
│   │  Azure Monitor ◄── Alert Rules ────────┘                          │   │     │
│   │       │         (restart count, no new blobs, CPU)                │   │     │
│   │       └──► Action Group ──► Email / Teams / PagerDuty             │   │     │
│   └───────────────────────────────────────────────────────────────────┘   │     │
│                                                                           │     │
│   ┌───────────────────────────────────────────────────────────────────┐   │     │
│   │  Azure Key Vault (radio-kv)                                       │   │     │
│   │  └── AZURE_STORAGE_CONNECTION_STRING                              │   │     │
│   │  └── stream credentials (if auth required)                        │   │     │
│   │  Accessed via Container Apps Managed Identity (no passwords)      │   │     │
│   └───────────────────────────────────────────────────────────────────┘   │     │
│                                                                           │     │
│   ┌───────────────────────────────────────────────────────────────────┐   │     │
│   │  Azure Container Registry (radiorecorderacr)                      │   │     │
│   │  └── radiorecorderacr.azurecr.io/radio-recorder:latest            │   │     │
│   └───────────────────────────────────────────────────────────────────┘   │     │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Resource Selection

### Compute — Azure Container Apps (ACA)

One Container App per radio station. Each app runs one replica permanently (`min-replicas=1`).

| Setting      | Value                                               |
| ------------ | --------------------------------------------------- |
| Image        | `radiorecorderacr.azurecr.io/radio-recorder:latest` |
| CPU          | 0.25 vCPU                                           |
| Memory       | 0.5 Gi                                              |
| Min replicas | 1                                                   |
| Max replicas | 1                                                   |
| Ingress      | Disabled (outbound only)                            |

### Storage — Azure Blob Storage (GPv2)

- Single storage account `radiorecordingssa`, LRS redundancy
- One logical prefix per station: `recordings/dsr/`, `recordings/ehrhiti/`
- Lifecycle management policy: Hot → Cool after 7 days → Archive after 30 days
- Soft delete: 14 days (protection against accidental deletion)

### Messaging — Azure Event Grid + Service Bus

- Blob Storage emits `BlobCreated` events to Event Grid automatically (no code needed)
- Event Grid filters `*.mp3` and forwards to Service Bus Queue `new-recordings`
- Service Bus provides: durable delivery, dead-letter queue, at-least-once semantics
- Downstream consumers subscribe to the queue independently of the recorders

### Secrets — Azure Key Vault

- Stores `AZURE_STORAGE_CONNECTION_STRING` and any stream credentials
- Container Apps access Key Vault via **Managed Identity** — no passwords, no rotation
- Secrets never appear in container environment variable definitions

### Monitoring — Azure Monitor + Log Analytics

- Container Apps automatically ship all stdout/stderr to the linked Log Analytics Workspace
- Alert rules:
  - Container restart count > 3 in 1 hour → stream likely dead
  - No new blobs in container in > 15 minutes → ffmpeg stuck silently
  - CPU > 80% sustained → capacity issue
- Action Group: email + Teams webhook

### Registry — Azure Container Registry (ACR)

- Private registry in same region → fast image pulls, no Docker Hub rate limits
- Basic tier ($5/month) is sufficient
- ACA pulls with Managed Identity — no registry credentials needed

---

## 3. Justification

| Resource       | Chosen                        | Alternatives                               | Why                                                                                                                                                                                               |
| -------------- | ----------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Compute**    | Azure Container Apps          | ACI, AKS, App Service, VM                  | ACA: serverless containers, `min-replicas=1` keeps recorder always-on, built-in Log Analytics integration, rolling deployments, no cluster management vs AKS, better lifecycle management vs ACI. |
| **Storage**    | Blob Storage GPv2             | Azure Files, Azure Disk                    | Blob is cheapest per-GB for object storage, native Event Grid integration, lifecycle tiers. Azure Files adds SMB overhead with no benefit here.                                                   |
| **Messaging**  | Event Grid + Service Bus      | Webhooks, Azure Queue Storage, Event Hubs  | SB Queue provides durable buffer + dead-letter queue. Webhooks lose events if consumer is down. Queue Storage lacks DLQ and advanced filtering. Event Hubs is overkill at this event volume.      |
| **Secrets**    | Azure Key Vault               | App-level env vars, custom secrets manager | Managed Identity access = zero credential rotation. Secrets never exposed in deployment configs.                                                                                                  |
| **Monitoring** | Azure Monitor + Log Analytics | Datadog, Grafana Cloud                     | Native Azure, zero additional agents, already integrated with Container Apps. Grafana can be layered on top via the Azure Monitor data source.                                                    |
| **Registry**   | ACR Basic                     | Docker Hub, GitHub Container Registry      | Private, same-region, Managed Identity pull, no rate limits.                                                                                                                                      |

---

## 4. Scaling — 1 → 20 Stations

Each station is an independent Container App sharing the same environment. No shared state between recorders.

### Terraform module for multi-station deployment

```hcl
# modules/recorder/main.tf
variable "station_name"   {}
variable "stream_url"     {}
variable "environment_id" {}
variable "registry"       {}
variable "kv_secret_uri"  {}
variable "identity_id"    {}
variable "location"       {}
variable "resource_group" {}

resource "azurerm_container_app" "recorder" {
  name                         = "recorder-${var.station_name}"
  resource_group_name          = var.resource_group
  container_app_environment_id = var.environment_id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.identity_id]
  }

  registry {
    server   = var.registry
    identity = var.identity_id
  }

  secret {
    name                = "storage-conn"
    key_vault_secret_id = var.kv_secret_uri
    identity            = var.identity_id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "recorder"
      image  = "${var.registry}/radio-recorder:latest"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "STATION_NAME"
        value = var.station_name
      }
      env {
        name  = "STREAM_URL"
        value = var.stream_url
      }
      env {
        name        = "AZURE_STORAGE_CONNECTION_STRING"
        secret_name = "storage-conn"
      }
      env {
        name  = "CHUNK_DURATION"
        value = "600"
      }
      env {
        name  = "LOG_LEVEL"
        value = "WARNING"
      }
    }
  }
}
```

```hcl
locals {
  stations = {
    dsr     = "https://stream.dimensionesuonoroma.radio/audio/dsr.stream_aac64/playlist.m3u8"
    ehrhiti = "https://stream3.ehrhiti.lv:8000/Stream_93_LV01.aac"
    jazz1   = "https://example.com/jazz.m3u8"
  }
}

module "recorder" {
  for_each = local.stations
  source   = "./modules/recorder"

  station_name   = each.key
  stream_url     = each.value
  environment_id = azurerm_container_app_environment.env.id
  registry       = azurerm_container_registry.acr.login_server
  kv_secret_uri  = azurerm_key_vault_secret.storage_conn.id
  identity_id    = azurerm_user_assigned_identity.recorder_id.id
  location       = var.location
  resource_group = azurerm_resource_group.rg.name
}
```

**Add a new station:** add one line to `locals.stations`, run `terraform apply`. Takes ~30 seconds.

**Remove a station:** remove the line, run `terraform apply`. Container App is deleted, recordings stay in Blob.

**ACA environment quota:** default limit is 20 apps per environment. For 20+ stations, either request a quota increase or split into 2 environments (e.g., `radio-env-a`, `radio-env-b`).

---

## 5. Cost Estimate — 20 Stations (West Europe, Pay-as-you-go)

### Azure Container Apps

- Free tier: 180,000 vCPU-seconds + 360,000 GB-seconds per month per subscription
- 20 apps × 0.25 vCPU × 730h × 3600s = **13.1M vCPU-seconds/month**
- Billable above free tier: ~12.9M × $0.000024 = **~$31/month**
- Memory: 20 × 0.5 GB × 730h × 3600s = **26.3M GB-seconds** → ~**$6/month**
- **Container Apps total: ~$37/month**

### Blob Storage

- 10-min chunks @ ~10 MB → 6/hour × 24h × 30d × 20 stations = **86,400 files**
- ~10 MB × 86,400 = **864 GB/month**
- Hot (0–7 days): ~288 GB × $0.018 = **$5.2**
- Cool (7–30 days): ~576 GB × $0.01 = **$5.8**
- Write operations: 86,400 × $0.05/10k = **$0.43**
- **Storage total: ~$11/month**

### Event Grid

- ~86,400 events/month → within free 100k/month tier → **$0**

### Service Bus Standard

- $0.10/million messages → **< $1/month**

### Log Analytics (at WARNING level)

- ffmpeg at WARNING = ~1 log line per reconnect, not per HLS segment
- Estimated ~50 MB/day total for 20 stations
- First 5 GB/day free → **$0**

> At INFO level: ~10 GB/day → ~$1,150/month. Always use `LOG_LEVEL=WARNING` in production.

### ACR Basic

- **$5/month**

### Key Vault

- ~$0.03/10k operations → **< $1/month**

---

### Monthly Total

| Component                    | Cost           |
| ---------------------------- | -------------- |
| Container Apps (20 stations) | ~$37           |
| Blob Storage                 | ~$11           |
| Log Analytics (WARNING)      | $0             |
| ACR Basic                    | $5             |
| Service Bus + Event Grid     | ~$1            |
| Key Vault                    | < $1           |
| **Total**                    | **~$55/month** |

---

## 6. Deployment Steps

### Option A — Azure CLI

```bash
# Variables
RG="radio-recorder-rg"
LOCATION="westeurope"
SA="radiorecordingssa"
ACR="radiorecorderacr"
KV="radio-kv"
LAW="radio-logs-law"
ENV="radio-recorder-env"

# 1. Resource group
az group create --name $RG --location $LOCATION

# 2. Container Registry + build image
az acr create --name $ACR --resource-group $RG --sku Basic --admin-enabled false
az acr build --registry $ACR --image radio-recorder:latest .

# 3. Storage account
az storage account create \
  --name $SA --resource-group $RG \
  --sku Standard_LRS --kind StorageV2

az storage container create --name recordings --account-name $SA

# 4. Log Analytics workspace
LAW_ID=$(az monitor log-analytics workspace create \
  --resource-group $RG --workspace-name $LAW \
  --query id -o tsv)

LAW_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group $RG --workspace-name $LAW \
  --query primarySharedKey -o tsv)

az containerapp env create \
  --name $ENV --resource-group $RG --location $LOCATION \
  --logs-workspace-id $LAW_ID \
  --logs-workspace-key $LAW_KEY

az keyvault create --name $KV --resource-group $RG --location $LOCATION

CONN=$(az storage account show-connection-string \
  --name $SA --resource-group $RG -o tsv)

az keyvault secret set --vault-name $KV \
  --name AzureStorageConnectionString --value "$CONN"

KV_SECRET_URI=$(az keyvault secret show \
  --vault-name $KV --name AzureStorageConnectionString \
  --query id -o tsv)


IDENTITY_ID=$(az identity create \
  --name radio-recorder-id --resource-group $RG \
  --query id -o tsv)

IDENTITY_CLIENT_ID=$(az identity show \
  --name radio-recorder-id --resource-group $RG \
  --query clientId -o tsv)

az keyvault set-policy --name $KV \
  --object-id $(az identity show --name radio-recorder-id \
    --resource-group $RG --query principalId -o tsv) \
  --secret-permissions get

# Deploy a station
az containerapp create \
  --name recorder-dsr \
  --resource-group $RG \
  --environment $ENV \
  --image "${ACR}.azurecr.io/radio-recorder:latest" \
  --user-assigned $IDENTITY_ID \
  --registry-identity $IDENTITY_ID \
  --registry-server "${ACR}.azurecr.io" \
  --min-replicas 1 --max-replicas 1 \
  --cpu 0.25 --memory 0.5Gi \
  --secrets "storage-conn=keyvaultref:${KV_SECRET_URI},identityref:${IDENTITY_ID}" \
  --env-vars \
      STATION_NAME=dsr \
      "STREAM_URL=https://stream.dimensionesuonoroma.radio/audio/dsr.stream_aac64/playlist.m3u8" \
      OUTPUT_DIR=/recordings \
      CHUNK_DURATION=600 \
      LOG_LEVEL=WARNING \
      "AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn"

az servicebus namespace create \
  --name radio-sb --resource-group $RG --location $LOCATION --sku Standard

az servicebus queue create \
  --name new-recordings --namespace-name radio-sb --resource-group $RG

SB_ID=$(az servicebus namespace show \
  --name radio-sb --resource-group $RG --query id -o tsv)

SA_ID=$(az storage account show \
  --name $SA --resource-group $RG --query id -o tsv)

az eventgrid event-subscription create \
  --name new-mp3-recordings \
  --source-resource-id $SA_ID \
  --endpoint-type servicebusqueue \
  --endpoint "${SB_ID}/queues/new-recordings" \
  --included-event-types Microsoft.Storage.BlobCreated \
  --subject-ends-with ".mp3"
```

### Option B — Terraform

```bash

git clone https://github.com/<smth>/radio-recorder.git
cd radio-recorder/terraform

terraform init

terraform plan \
  -var="location=westeurope" \
  -var="acr_name=radiorecorderacr"

terraform apply \
  -var="location=westeurope" \
  -var="acr_name=radiorecorderacr"

# Add a new station later
```