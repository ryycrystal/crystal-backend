# Crystal Backend

The indexer + API behind the Crystal launchpad. It ingests Monad chain logs, derives
token/trade/holder/position state into Postgres, and serves that state to the frontend
over REST and a WebSocket.

This README is the operator's guide: how to run it locally, test it, deploy it, and
keep it healthy in production. For the deep architecture, read
[`ARCHITECTURE.md`](ARCHITECTURE.md). For the full indexer start-mode runbook (bootstrap,
rebuild, replay, snapshots), read [`STARTUP_MODES.md`](STARTUP_MODES.md) — this file
points at it rather than duplicating it.

> **New here? Read these three sections first:** [Mental model](#1-mental-model),
> [Local development](#3-local-development), [Deploying](#6-deploying-to-production).
> Then skim [Gotchas](#9-gotchas-learned-the-hard-way) before you touch prod.

---

## 1. Mental model

There are **two processes built from one image**:

| Process | Entrypoint | What it does | Replicas |
|---|---|---|---|
| **Indexer** | `indexer_main.py` | Follows the chain, writes derived state to Postgres. Owns all DB migrations. | Exactly **1** (guarded by a Postgres advisory lock) |
| **API** | `main.py` (`uvicorn main:app`) | Reads Postgres, serves REST + WebSocket. Never talks to the indexer. | 1–3 (autoscales) |

They share nothing but the database. The API is stateless and horizontally scalable;
the indexer must be a singleton. Everything flows one direction:

```
Monad RPC → indexer → Postgres → API → frontend
```

**Key consequence for deploys:** the indexer owns `init_db()` (schema + migrations),
so when a change adds a column, **deploy the indexer first** so the migration lands
before the API queries the new column. See [Deploying](#6-deploying-to-production).

---

## 2. Prerequisites

- **Python 3.12** (the code targets 3.12; `match`/typing features assume it).
- **PostgreSQL 14+** — a local instance for dev, Azure Postgres Flexible Server in prod.
- **Azure CLI** with the `containerapp` extension, for deploys (`az extension add --name containerapp --upgrade`).
- **Docker** — only needed if you want to build/run the image locally; deploys build in the cloud (ACR), so Docker is optional.

Install Python deps:

```powershell
pip install -r requirements.txt
pip install pytest ruff        # dev tooling, not in requirements.txt
```

`requirements.txt` is intentionally minimal (what ships in the image): `fastapi`,
`uvicorn`, `websockets`, `httpx`, `psycopg2-binary`.

---

## 3. Local development

### 3.1 Configure the database connection

Storage reads **either** `DATABASE_URL` **or** the individual `PG*` vars
(`core/storage/base.py`). `DATABASE_URL` wins if set. For local work, one variable is
easiest.

There is a helper, `dev-env.ps1`, that points you at the **local** dev DB
(`crystal_dev`) so you never accidentally run against prod:

```powershell
. .\dev-env.ps1     # sets $env:DATABASE_URL -> localhost:5432/crystal_dev
```

Create the local DB once:

```powershell
psql -U postgres -c "CREATE DATABASE crystal_dev;"
```

The indexer creates its own schema on first run via `init_db()`; you don't run
migrations by hand.

> **Never point local runs at the prod DB.** `dev-env.ps1` exists specifically to keep
> `crystal_dev` and prod isolated. Prod credentials live in the Azure Container App
> secrets, not in this repo.

### 3.2 Run the API locally

```powershell
. .\dev-env.ps1
python main.py                 # or: uvicorn main:app --host 0.0.0.0 --port 8000
```

Then hit `http://localhost:8000/health` and `http://localhost:8000/openapi.json` (the
full route list). The API opens its Postgres pool on startup and closes it on shutdown.

### 3.3 Run the indexer locally

For local debugging you typically want live-only, no dump, and the advisory lock
disabled (so it doesn't fight the prod indexer if you're pointed at a shared DB —
though you should be on `crystal_dev`):

```powershell
. .\dev-env.ps1
python indexer_main.py --mode live --no-indexer-lock
```

`python indexer_main.py --help` lists every flag. The full mode reference (bootstrap /
resume / rebuild / replay-only / live), the raw-log dump workflow, and snapshots are in
[`STARTUP_MODES.md`](STARTUP_MODES.md). **Read that before doing anything beyond `live`.**

---

## 4. Testing

Tests are split in two:

- **Unit / pure-logic tests** run with no database.
- **Integration tests** need Postgres and **skip themselves** unless `TEST_DATABASE_URL`
  is set. They create and drop a scratch database named `crystal_lp_itest`, so the URL
  you pass must point at a server where the connecting user can `CREATE DATABASE`
  (point it at the `postgres` maintenance DB — the tests swap the DB name themselves).

Run everything against the local server:

```powershell
$env:TEST_DATABASE_URL = "postgresql://postgres:PASSWORD@localhost:5432/postgres?sslmode=disable"
python -m pytest -q
```

Run a single file or test:

```powershell
python -m pytest tests/test_ws_channels.py -q
python -m pytest tests/test_stats_shape.py::test_query_is_echoed_in_applied_filters -q
```

Notes:

- The integration suite terminates stale connections and drops `crystal_lp_itest` on
  setup, so an interrupted run doesn't poison the next one. If you ever see
  `ObjectInUse`, a previous run left a connection open — just re-run; setup will clear it.
- The WebSocket tests start the hub's background fanout task and cancel it on teardown.
- **~190+ tests should pass.** A green suite is the bar before any deploy.

---

## 5. Linting & formatting (the gates)

`ruff` is the enforced gate — both a linter and the formatter. Config is in
`pyproject.toml` (the `include` list is "everything that ships + tests"; dev-only
scripts like `export_logs.py` are deliberately out of scope).

```powershell
python -m ruff check .            # lint
python -m ruff format .           # apply formatting
python -m ruff format --check .   # verify formatting without changing files
```

**Before any deploy, all three must be clean.** Check the *exit code*, not the printed
output:

```powershell
python -m ruff check .;         if ($?) { "check OK" }
python -m ruff format --check .; if ($?) { "format OK" }
```

> **Do not pipe ruff to `tail`/`head` and rely on `&&`** — you'll read the pager's exit
> code instead of ruff's and unformatted code will sail through. This bit us; check the
> real exit code.

There's a narrow `mypy` config too (`core/lifecycle.py`, `core/adapters/base.py`,
`core/adapters/native.py`) — a green subset that's meant to expand over time, not a
whole-repo gate.

---

## 6. Deploying to production

Production runs two Azure Container Apps in resource group **`crystal-prod-rg`**, both
pulling from ACR **`crystalprodacr`**:

| App | Command in the image | Purpose |
|---|---|---|
| `crystal-api` | `uvicorn main:app` | REST + WS, `min-replicas 1 max-replicas 3` |
| `crystal-indexer` | `python indexer_main.py --mode resume …` | singleton, `min/max-replicas 1` |

The **first-time infrastructure setup** (creating the RG, ACR, environment, Azure Files
dump mount, and the two apps) is fully scripted in
[`STARTUP_MODES.md` → Azure Container Apps](STARTUP_MODES.md#azure-container-apps). You
only do that once. What follows is the **day-to-day "ship a code change" flow.**

### 6.1 Critical facts to get right

- **Resource group:** `crystal-prod-rg`
- **ACR short name (for `az acr build`):** `crystalprodacr`
- **ACR login server (for image references in `containerapp update`):**
  `crystalprodacr-c3dbbeh2exdec8av.azurecr.io` — **not** `crystalprodacr.azurecr.io`.
  Using the short `.azurecr.io` form in an image reference fails with
  `failed to resolve registry … no such host`. Always use the full login server.
- **API URL:** `https://crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io`
- **On Windows, prefix `az` with `PYTHONIOENCODING=utf-8`** (or set it in the shell) —
  the Azure CLI crashes on a `UnicodeEncodeError` writing its progress spinner to a
  cp1252 console otherwise. From Git Bash: `PYTHONIOENCODING=utf-8 az …`.

### 6.2 The deploy flow

Run from the `crystal backend` directory.

**Step 1 — verify the tree is clean.**

```bash
python -m ruff check .;         # exit 0
python -m ruff format --check .; # exit 0
TEST_DATABASE_URL="postgresql://postgres:PASSWORD@localhost:5432/postgres?sslmode=disable" \
  python -m pytest -q            # all green
```

**Step 2 — build the image in ACR** (builds in the cloud from your working tree; no
local Docker needed). Tag it with something descriptive *and* move `latest`:

```bash
PYTHONIOENCODING=utf-8 az acr build --registry crystalprodacr \
  --image crystal-backend:myfeature-YYYYMMDD \
  --image crystal-backend:latest .
```

**Step 3 — deploy the INDEXER first if the change adds/alters a DB column** (it runs
the migration). If the change is API-only, you can skip straight to the API.

```bash
SRV=crystalprodacr-c3dbbeh2exdec8av.azurecr.io
PYTHONIOENCODING=utf-8 az containerapp update \
  --name crystal-indexer --resource-group crystal-prod-rg \
  --image $SRV/crystal-backend:myfeature-YYYYMMDD \
  -o tsv --query "properties.latestRevisionName"
```

Wait for it to be healthy and confirm it's still advancing blocks before touching the API:

```bash
# health of the new revision
PYTHONIOENCODING=utf-8 az containerapp revision show \
  -n crystal-indexer -g crystal-prod-rg --revision <new-rev> \
  -o tsv --query "properties.healthState"

# blocks are advancing (watermark climbs between two calls)
API=https://crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io
curl -s "$API/tokens?since_block=1" | python -c "import sys,json;print(json.load(sys.stdin)['as_of_block'])"
```

**Step 4 — deploy the API.**

```bash
PYTHONIOENCODING=utf-8 az containerapp update \
  --name crystal-api --resource-group crystal-prod-rg \
  --image $SRV/crystal-backend:myfeature-YYYYMMDD \
  -o tsv --query "properties.latestRevisionName"
```

**Step 5 — verify the live revision is actually serving your image**, then smoke-test:

```bash
PYTHONIOENCODING=utf-8 az containerapp revision list -n crystal-api -g crystal-prod-rg \
  -o tsv --query "[?properties.trafficWeight>\`0\`].{n:name,h:properties.healthState,img:properties.template.containers[0].image}"

curl -s -o /dev/null -w "%{http_code}\n" "$API/health"
```

> **Verify against the revision that's actually taking traffic.** During a rollout the
> old revision can still be serving; confirm `trafficWeight` is on the new one and the
> `img` matches the tag you just built. More than once a "it didn't work" turned out to
> be checking a deprovisioning revision.

### 6.3 Rollback

Revisions are immutable and retained. To roll back, point traffic at the previous
revision's image (redeploy the old tag) or use the revision list to find the last-good
`img` and `az containerapp update --image` back to it.

---

## 7. Operating production

**Check what's live:**

```bash
PYTHONIOENCODING=utf-8 az containerapp revision list -n crystal-api -g crystal-prod-rg \
  -o tsv --query "[?properties.trafficWeight>\`0\`].{n:name,h:properties.healthState,img:properties.template.containers[0].image}"
PYTHONIOENCODING=utf-8 az containerapp revision list -n crystal-indexer -g crystal-prod-rg \
  -o tsv --query "[?properties.active].{n:name,h:properties.healthState,img:properties.template.containers[0].image}"
```

**Logs** (`--tail` max is **300** — larger values error out; the block-processing log is
very chatty so filter):

```bash
PYTHONIOENCODING=utf-8 az containerapp logs show -n crystal-indexer -g crystal-prod-rg --tail 300
PYTHONIOENCODING=utf-8 az containerapp logs show -n crystal-api -g crystal-prod-rg --tail 300
```

**Is the indexer keeping up?** The API surfaces the indexer watermark; watch it climb:

```bash
API=https://crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io
curl -s "$API/tokens?since_block=1" | python -c "import sys,json;print(json.load(sys.stdin)['as_of_block'])"
```

**Scale the API** (e.g. under load — it's safe, each replica runs its own WS hub and
serves its own clients independently):

```bash
PYTHONIOENCODING=utf-8 az containerapp update -n crystal-api -g crystal-prod-rg --min-replicas 2 --max-replicas 3
# …and back down
PYTHONIOENCODING=utf-8 az containerapp update -n crystal-api -g crystal-prod-rg --min-replicas 1 --max-replicas 3
```

**Never scale the indexer past 1 replica.** It's a singleton; the Postgres advisory
lock will make a second replica idle, but don't rely on that — keep `min=max=1`.

**Restarting the indexer** (e.g. after a config change) triggers `init_db()` migrations
and the startup backfills; those are idempotent, so a restart just re-verifies and
catches back up from the last processed block. Safe.

---

## 8. Environment variables

Read by the code (`core/storage/base.py`, `core/chain.py`, `modules/nadfun.py`, etc.):

| Var | Purpose | Prod value / note |
|---|---|---|
| `DATABASE_URL` | Full Postgres URL; overrides the `PG*` parts if set | used locally via `dev-env.ps1` |
| `PGHOST` `PGUSER` `PGPASSWORD` `PGPORT` `PGDATABASE` `PGSSLMODE` | Postgres connection parts | prod = Azure Postgres, `PGSSLMODE=require` |
| `DB_MIN_CONN` / `DB_MAX_CONN` | Connection pool bounds | `1` / `25` |
| `RPC_HTTP` | Monad JSON-RPC endpoint (indexer) | `https://rpc.monad.xyz` |
| `URI_RECOVERY_BATCH` | Tokens per pass the metadata sweep recovers from chain | default 100; raised to 400 during a backfill |
| `METADATA_BATCH_SIZE` / `METADATA_LOG_EVERY` | Metadata worker tuning | defaults are fine |
| `RPC_EXPORT_RPS` | Rate limit for the standalone dump exporter | dev tooling only |
| `X_BEARER_TOKEN` | Twitter/X API token for the `/x` route | optional |

In production these are set as Container App **env vars and secrets** (`PGPASSWORD` and
`RPC_HTTP` are secrets: `PGPASSWORD=secretref:pgpassword`). **Secrets are not in this
repo** — they live in the Container App configuration. To view/rotate:
`az containerapp secret list -n crystal-api -g crystal-prod-rg`.

---

## 9. Gotchas learned the hard way

These are real incidents from building this. Read them once; they'll save you an outage.

- **Migration ordering.** The indexer runs `init_db()` and owns all schema. If you
  deploy the API before the indexer for a change that adds a column, the API 500s until
  the indexer migrates. **Indexer first for schema changes.** (This caused a ~4-minute
  outage once.)
- **Migrations must not do heavy work inside `init_db()`.** A table-wide `UPDATE`
  wedged between `ALTER TABLE` statements once deadlocked startup. Backfills are
  separate, batched functions (`backfill_cost_basis`, `backfill_realized_pnl`) using
  `FOR UPDATE SKIP LOCKED`, called after `init_db()`. Follow that pattern for any new
  data migration.
- **Verify the serving revision, not the newest.** A rollout leaves the old revision
  briefly serving; always confirm `trafficWeight` and the image tag.
- **`az` + Windows console = `UnicodeEncodeError`.** Prefix with `PYTHONIOENCODING=utf-8`.
- **ACR login server is the long one** (`crystalprodacr-c3dbbeh2exdec8av.azurecr.io`),
  not `crystalprodacr.azurecr.io`, for image references.
- **`az containerapp logs --tail` caps at 300.** Larger values return an error, not more
  logs — don't mistake the error for "no output."
- **Don't trust ruff behind a pipe.** `ruff check . | tail` masks the exit code; the
  gate silently passes. Check `$?`.
- **Measuring latency from a laptop is mostly RTT.** The API is in Japan East (~115ms
  round trip with keep-alive). A "slow endpoint" measured with fresh `curl` connections
  is usually just TCP+TLS setup, not the server. Use keep-alive, or `EXPLAIN ANALYZE`
  against the DB for real server-side cost.
- **Monad has single-slot finality — no reorgs.** The reorg path was removed. Don't
  reintroduce reorg-handling assumptions.

---

## 10. Repo map (quick reference)

Full detail in [`ARCHITECTURE.md`](ARCHITECTURE.md). The short version:

```
main.py               API entrypoint (uvicorn main:app)
indexer_main.py       indexer entrypoint (--mode …); see STARTUP_MODES.md
state.py              the write path: every apply_* handler, in-memory world
core/
  chain.py            contract addresses, event-sig → tag, tag → parser, log gating
  sequencer.py        per-block ordering, batched writes, records processed blocks
  stream.py           live log follower + 30s vault sampler
  lifecycle.py        source-agnostic phase/progress rules
  oracle.py           MON/USD from V3 swaps
  adapters/           per-source curve geometry (native.py, nadfun.py)
  storage/            all SQL; schema.py owns DDL + idempotent migrations
modules/              pure ABI decoders per protocol (launchpad, nadfun, markets, …)
api/
  api.py              app + shared serializers/helpers (import order is load-bearing)
  routes/             FastAPI routers: launchpad, markets, pools, vaults, x
  ws.py               WebSocket hub (7 channels) + transport
  ws_data.py          synchronous DB fetchers for the WS channels
tests/                pytest; integration tests need TEST_DATABASE_URL
STARTUP_MODES.md      indexer start-mode runbook + first-time Azure setup
ARCHITECTURE.md       deep architecture
```

**The two list endpoints** (`/tokens`, `/search/query`) and the token detail data (the
`/stats`, `/holders`, `/token/{addr}/trades`, chart endpoints, plus the 7 WS channels)
are what the frontend's board/meme/explorer pages consume. `/search/query` supports
GET and POST (POST for blacklists past ~400 entries) and echoes `applied_filters` so a
misspelled param can't silently return an unfiltered page.

---

## 11. Known open items (as of handoff)

Things that are **not** code bugs but need a human decision or action before / around
launch:

- **V₀ is 5 MON on-chain** (set low for testing). It must be restored to **1000** before
  go-live via the `changeLaunchpadParams` governance transaction. This needs the owner
  key — it is an on-chain action, not a backend deploy.
- **The `insider` filter definition is provisional.** It flags wallets holding more than
  they net-bought (excess arrived by transfer). This catches ordinary airdrop
  recipients too, so the label is a product call — revisit before it's prominent in the UI.
  Definition lives in one place in `core/storage/launchpad.py` (`search_tokens_filtered`).
- **End-to-end trade execution on nad.fun v1/v2 has not been tested.** The indexer
  correctly *ingests* all three paths (native, v1, v2), and native buys + a wash-trade
  bot are exercised — but a real buy/sell round trip through the v1 and v2 contracts
  hasn't been run. Do a deliberate manual pass on each path before launch.
- **Metadata self-heals from chain** for older tokens missing images (a background sweep
  in the indexer). A small residue of tokens whose metadata host is permanently dead
  will never resolve and will show a placeholder — that's expected, not a stall.

---

## 12. Where to look when something's wrong

| Symptom | First place to look |
|---|---|
| API returning 500s right after a deploy | Did you deploy API before indexer for a schema change? Roll API back, let indexer migrate, redeploy. |
| A number on a card is wrong | It's derived in `api/api.py` (`_batch_serialize_tokens`, the `_batch_get_*` helpers) or `api/ws_data.py`. REST and WS should agree — if they don't, that's the bug. |
| Indexer not advancing | `az containerapp logs show -n crystal-indexer --tail 300`; check RPC reachability and the advisory lock. |
| A filter "does nothing" | Check `applied_filters` in the `/search/query` response — if your param isn't listed, it was misspelled/unknown and silently ignored. |
| Tests hang or `ObjectInUse` | A prior run left a DB connection; re-run — setup terminates stragglers. |

---

*Questions the code can't answer: start with [`ARCHITECTURE.md`](ARCHITECTURE.md), then
[`STARTUP_MODES.md`](STARTUP_MODES.md). The frontend ↔ backend coordination log is
`../crystal misc/backend_tasks.md`.*
