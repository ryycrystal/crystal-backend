# Indexer Startup Runbook

Default relevant start block: `37709836`.

Replay always starts here. Contract generations are selected by address, not by
block: only the addresses in `ADDRS` are indexed, so a full replay rebuilds
launchpad history from the start while picking up the current Crystal router and
vault factory from their own deploy blocks (`92718537` / `92718579`). Retired
router generations are dropped simply by not being listed.

Postgres stores derived query state. Two things can rebuild it: the
`launchpad_block_logs` table, which caches raw logs back to `37709836`, and the
on disk dump, which only reaches back to `89746624`. The cache is the wider
source, so `--mode reindex` replays from it and the dump is only a secondary
copy. A Postgres snapshot is a fast restore point, but it does not replace
either.

## Migrating to a new Crystal deployment

Nadfun history survives untouched, the retired router's data disappears, and the
new router is indexed from its own deploy block. No code change and no image
rebuild are needed.

Two filters do the work. The cache is written address filtered, so it holds every
nadfun log plus whichever Crystal generation was live at the time. At replay time
`accepts_log_for_indexing` filters again against the current `ADDRS`, so logs from
a retired router are rejected on the way in and never reach Postgres. The new
router's logs are the one gap: it was not being indexed when those blocks were
first seen, so nothing was cached for it and that range has to be refetched.

1. Point the indexer at the new contracts, via the container app environment:

```powershell
CRYSTAL_ADDRESS=0x...
VAULT_FACTORY_ADDRESS=0x...
```

2. Evict the cached logs from the new deploy block onward. They were written
   while the old addresses were current, so they are missing the new router's
   events. Deleting them makes the RPC catchup refetch the range unfiltered:

```sql
DELETE FROM launchpad_block_logs WHERE number >= <new deploy block>;
```

3. Rebuild:

```powershell
python indexer_main.py --mode reindex --clean --start-block 37709836
```

`reindex` replays cached logs only and skips blocks absent from the cache; a block
with no cached entry held nothing indexable. When it reaches the end of the cache
it hands off to the live path, which refetches the evicted range over RPC and
picks up the new contracts, then continues streaming.

Find the deploy block by bisecting `eth_getCode` over the address; the first block
that returns bytecode is the one to use.

`--clean` is what forces the wipe and restart. Without it `reindex` resumes from
the last block already written, which is the behaviour you want for an ordinary
restart part way through a long replay.

The full replay from `37709836` is unavoidable today: aggregate tables such as
`launchpad_positions` and `launchpad_users` hold running totals whose realized PnL
and cost basis are path dependent, so a later generation's contribution cannot be
subtracted with SQL. Making `clear_derived_state_from_block` honour its block
argument would allow a partial rebuild, but that also needs a way to recompute
those aggregates; see the `block-scoped-clear` branch.

## VS Code Markdown Preview

```text
Ctrl+Shift+V
```

Preview to the side:

```text
Ctrl+K, then V
```

## Environment

Set production DB and RPC values before running services:

```powershell
$env:PGHOST = "crystal-prod-db.postgres.database.azure.com"
$env:PGUSER = "crystaladmin"
$env:PGPORT = "5432"
$env:PGDATABASE = "crystal"
$env:PGPASSWORD = "YOUR_PASSWORD"
$env:PGSSLMODE = "require"
$env:RPC_HTTP = "https://rpc.monad.xyz"
$env:DB_MIN_CONN = "1"
$env:DB_MAX_CONN = "25"
```

`DATABASE_URL` also works if you prefer one variable:

```powershell
$env:DATABASE_URL = "postgresql://crystaladmin:YOUR_PASSWORD@crystal-prod-db.postgres.database.azure.com:5432/crystal?sslmode=require"
```

Override deployed contract addresses when needed:

```powershell
$env:CRYSTAL_ADDRESS = "0x..."
$env:VAULT_FACTORY_ADDRESS = "0x..."
$env:NADFUN_ADDRESS = "0x..."
```

Legacy aliases also work:

```powershell
$env:ROUTER_ADDRESS = "0x..."
$env:VAULTS_ADDRESS = "0x..."
```

## Initial Raw Dump

Export all logs from the first relevant block to current head:

```powershell
python export_logs.py 37709836 --out chain-log-dump --batch 100 --rps 10
```

Export only backend-indexed topics:

```powershell
python export_logs.py 37709836 --out indexed-log-dump --indexed-topics --batch 100 --rps 10
```

Export a bounded range:

```powershell
python export_logs.py 37709836 --end 38000000 --out chain-log-dump --batch 100 --rps 10
```

Resume an interrupted dump:

```powershell
python export_logs.py 37709836 --out chain-log-dump --batch 100 --rps 10
```

Run a standalone continuous full-log dump follower:

```powershell
python export_logs.py 37709836 --out chain-log-dump --batch 100 --rps 5 --follow
```

Use larger JSON-RPC batches for timestamp stamping:

```powershell
python export_logs.py 37709836 --out chain-log-dump --batch 100 --timestamp-batch 100 --rps 10
```

## First Start

Use `bootstrap` when the DB has no useful derived state:

```powershell
python indexer_main.py --mode bootstrap --dump-dir chain-log-dump --live-dump-dir chain-log-dump --start-block 37709836
```

Behavior:

- Clears derived tables.
- Replays the dump from `--start-block`.
- Starts live RPC/WebSocket indexing after the dump.
- Keeps appending full-chain logs to `--live-dump-dir`.

Bootstrap without a dump:

```powershell
python indexer_main.py --mode bootstrap --start-block 37709836
```

Disable the single-indexer Postgres advisory lock only for local debugging:

```powershell
python indexer_main.py --mode resume --dump-dir chain-log-dump --no-indexer-lock
```

## Normal Restart Or Deploy

Use `resume` for normal production restarts:

```powershell
python indexer_main.py --mode resume --dump-dir chain-log-dump --live-dump-dir chain-log-dump
```

Behavior:

- Loads derived state from Postgres.
- Reads `launchpad_blocks` to find the last processed block.
- Replays local dump chunks after that block if available.
- Starts live RPC/WebSocket indexing after DB plus dump catchup.
- Keeps appending full-chain logs while live.

Resume without live dump follower:

```powershell
python indexer_main.py --mode resume --dump-dir chain-log-dump
```

## Full Rebuild

Use `rebuild` when old derived state is wrong and snapshots cannot be trusted:

```powershell
python indexer_main.py --mode rebuild --dump-dir chain-log-dump --live-dump-dir chain-log-dump --start-block 37709836
```

Behavior:

- Clears derived tables.
- Replays raw dump with the current code.
- Starts live indexing after the dump.
- Keeps appending full-chain logs while live.

Rebuild only through a specific dump block:

```powershell
python indexer_main.py --mode rebuild --dump-dir chain-log-dump --dump-end 38000000 --start-block 37709836
```

## Offline Replay Only

Replay dump into derived Postgres state and exit:

```powershell
python indexer_main.py --mode replay-only --dump-dir chain-log-dump --start-block 37709836
```

Replay a bounded range:

```powershell
python indexer_main.py --mode replay-only --dump-dir chain-log-dump --start-block 37709836 --dump-end 38000000
```

Direct replay script equivalent:

```powershell
python replay_dump.py --dump chain-log-dump --start 37709836 --reset
```

Direct replay for a bounded range:

```powershell
python replay_dump.py --dump chain-log-dump --start 37709836 --end 38000000 --reset
```

Allow old dumps missing `blockTimestamp`:

```powershell
python replay_dump.py --dump chain-log-dump --start 37709836 --reset --allow-missing-timestamps
```

## Live Only

Use `live` when intentionally ignoring local dump catchup:

```powershell
python indexer_main.py --mode live
```

Live from a specific fallback start block if DB has no processed block:

```powershell
python indexer_main.py --mode live --start-block 37709836
```

Live plus full-log dump follower:

```powershell
python indexer_main.py --mode live --live-dump-dir chain-log-dump
```

## Snapshots

Create a derived Postgres state snapshot:

```powershell
python db_snapshot.py create --out snapshots
```

Create a labeled snapshot:

```powershell
python db_snapshot.py create --out snapshots --label before_rebuild
```

Restore a snapshot:

```powershell
python db_snapshot.py restore snapshots\your_snapshot.dump
```

Restore with parallel jobs:

```powershell
python db_snapshot.py restore snapshots\your_snapshot.dump --jobs 4
```

Restore then resume from dump and live:

```powershell
python db_snapshot.py restore snapshots\your_snapshot.dump
python indexer_main.py --mode resume --dump-dir chain-log-dump --live-dump-dir chain-log-dump
```

## Replay Benchmark

Benchmark a small replay range into Postgres:

```powershell
python replay_benchmark.py --dump chain-log-dump --start 37709836 --end 37719836 --reset
```

Benchmark a later range without clearing derived state:

```powershell
python replay_benchmark.py --dump chain-log-dump --start 38000000 --end 38010000
```

The output includes `blocks_per_sec`. For production sync, run multiple ranges
with realistic logs and compare the rate before changing Azure DB tiers.

## Docker

Build the image locally:

```powershell
docker build -t crystal-backend:local .
```

Run the API container locally:

```powershell
docker run --rm -p 8000:8000 --env PGHOST="$env:PGHOST" --env PGUSER="$env:PGUSER" --env PGPORT="$env:PGPORT" --env PGDATABASE="$env:PGDATABASE" --env PGPASSWORD="$env:PGPASSWORD" --env PGSSLMODE="$env:PGSSLMODE" crystal-backend:local
```

Run the indexer from the same image:

```powershell
docker run --rm --env PGHOST="$env:PGHOST" --env PGUSER="$env:PGUSER" --env PGPORT="$env:PGPORT" --env PGDATABASE="$env:PGDATABASE" --env PGPASSWORD="$env:PGPASSWORD" --env PGSSLMODE="$env:PGSSLMODE" --env RPC_HTTP="$env:RPC_HTTP" crystal-backend:local python indexer_main.py --mode resume --dump-dir chain-log-dump --live-dump-dir chain-log-dump
```

## Azure Container Apps

Use two Container Apps from the same image:

- API app: runs `uvicorn main:app`, can have multiple replicas.
- Indexer app: runs `indexer_main.py`, keep `min-replicas 1` and
  `max-replicas 1`. The Postgres advisory lock is still enabled as a guard.

Install/update Azure CLI support:

```powershell
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.Storage
```

Set deployment variables:

```powershell
$env:AZ_LOCATION = "japaneast"
$env:AZ_RG = "crystal-prod-rg"
$env:AZ_ENV = "crystal-prod-env"
$env:AZ_ACR = "crystalprodacr"
$env:AZ_IMAGE = "$env:AZ_ACR.azurecr.io/crystal-backend:latest"
$env:AZ_STORAGE = "crystalproddump"
$env:AZ_SHARE = "chain-log-dump"
$env:AZ_STORAGE_MOUNT = "chainlogdump"
```

Create resource group, registry, and Container Apps environment:

```powershell
az group create --name $env:AZ_RG --location $env:AZ_LOCATION
az acr create --resource-group $env:AZ_RG --name $env:AZ_ACR --sku Basic
az acr build --registry $env:AZ_ACR --image crystal-backend:latest .
az containerapp env create --name $env:AZ_ENV --resource-group $env:AZ_RG --location $env:AZ_LOCATION
```

Enable ACR pull credentials for the first deploy:

```powershell
az acr update --name $env:AZ_ACR --admin-enabled true
$env:AZ_ACR_USERNAME = az acr credential show --name $env:AZ_ACR --query "username" -o tsv
$env:AZ_ACR_PASSWORD = az acr credential show --name $env:AZ_ACR --query "passwords[0].value" -o tsv
```

Create persistent Azure Files storage for the raw dump:

```powershell
az storage account create --name $env:AZ_STORAGE --resource-group $env:AZ_RG --location $env:AZ_LOCATION --sku Standard_LRS
az storage share-rm create --resource-group $env:AZ_RG --storage-account $env:AZ_STORAGE --name $env:AZ_SHARE --quota 1024
$env:AZ_STORAGE_KEY = az storage account keys list --resource-group $env:AZ_RG --account-name $env:AZ_STORAGE --query "[0].value" -o tsv
az containerapp env storage set --name $env:AZ_ENV --resource-group $env:AZ_RG --storage-name $env:AZ_STORAGE_MOUNT --storage-type AzureFile --azure-file-account-name $env:AZ_STORAGE --azure-file-account-key $env:AZ_STORAGE_KEY --azure-file-share-name $env:AZ_SHARE --access-mode ReadWrite
```

Create the API app:

```powershell
az containerapp create --name crystal-api --resource-group $env:AZ_RG --environment $env:AZ_ENV --image $env:AZ_IMAGE --registry-server "$env:AZ_ACR.azurecr.io" --registry-username $env:AZ_ACR_USERNAME --registry-password $env:AZ_ACR_PASSWORD --target-port 8000 --ingress external --min-replicas 1 --max-replicas 3 --secrets pgpassword="$env:PGPASSWORD" rpc-http="$env:RPC_HTTP" --env-vars PGHOST="$env:PGHOST" PGUSER="$env:PGUSER" PGPORT="$env:PGPORT" PGDATABASE="$env:PGDATABASE" PGSSLMODE="$env:PGSSLMODE" PGPASSWORD=secretref:pgpassword RPC_HTTP=secretref:rpc-http DB_MIN_CONN=1 DB_MAX_CONN=25
```

Create the indexer app scaled to zero until the dump mount is attached:

```powershell
az containerapp create --name crystal-indexer --resource-group $env:AZ_RG --environment $env:AZ_ENV --image $env:AZ_IMAGE --registry-server "$env:AZ_ACR.azurecr.io" --registry-username $env:AZ_ACR_USERNAME --registry-password $env:AZ_ACR_PASSWORD --min-replicas 0 --max-replicas 1 --secrets pgpassword="$env:PGPASSWORD" rpc-http="$env:RPC_HTTP" --env-vars PGHOST="$env:PGHOST" PGUSER="$env:PGUSER" PGPORT="$env:PGPORT" PGDATABASE="$env:PGDATABASE" PGSSLMODE="$env:PGSSLMODE" PGPASSWORD=secretref:pgpassword RPC_HTTP=secretref:rpc-http DB_MIN_CONN=1 DB_MAX_CONN=25 --command python --args indexer_main.py --mode resume --dump-dir /dump --live-dump-dir /dump --start-block 37709836
```

Mount the Azure Files dump share on the indexer:

```powershell
az containerapp show --name crystal-indexer --resource-group $env:AZ_RG -o yaml > crystal-indexer.yaml
```

Edit `crystal-indexer.yaml` so the template contains:

```yaml
template:
  volumes:
    - name: chain-log-dump
      storageType: AzureFile
      storageName: chainlogdump
  containers:
    - name: crystal-indexer
      volumeMounts:
        - volumeName: chain-log-dump
          mountPath: /dump
```

Apply the mount:

```powershell
az containerapp update --name crystal-indexer --resource-group $env:AZ_RG --yaml crystal-indexer.yaml
```

After the mount is applied, scale the indexer up:

```powershell
az containerapp update --name crystal-indexer --resource-group $env:AZ_RG --min-replicas 1 --max-replicas 1
```

## Checks

Compile-check changed Python files:

```powershell
python -m py_compile indexer_main.py replay_dump.py replay_benchmark.py export_logs.py db_snapshot.py core\chain.py core\sequencer.py core\storage\launchpad.py core\storage\base.py api\routes\vaults.py api\routes\markets.py api\api.py
```

Show indexer flags:

```powershell
python indexer_main.py --help
```

Show dump exporter flags:

```powershell
python export_logs.py --help
```

Show replay flags:

```powershell
python replay_dump.py --help
```

Show snapshot flags:

```powershell
python db_snapshot.py --help
```

Run tests when `pytest` is installed:

```powershell
python -m pytest
```

## Safety Notes

Dump replay validates contiguous chunk coverage. If a dump starts after the
requested replay block or has a missing chunk gap, startup refuses instead of
silently creating broken derived state.

Replay requires stamped timestamps by default and does not call RPC. Use
`--allow-missing-timestamps` only for old dumps where zero timestamps are
acceptable.
