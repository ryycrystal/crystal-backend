# Crystal backend — working notes

Written by the agents that have worked in this repo. It is cumulative: correct things you find to be wrong, and add what you learn. Prefer editing a section over appending a near-duplicate one.

Everything below is stated as of the date in the section heading where one is given. Verify anything load-bearing before you rely on it — several entries exist precisely because an earlier assumption turned out to be false.

---

## 1. What this repo is, and what it is not

This repo is the Crystal DEX backend on Monad (chain id 143). It contains **two services that run as separate deployments from the same image**:

- **`crystal-api`** — the FastAPI read API (`api/`).
- **`crystal-indexer`** — the chain indexer (`core/sequencer.py`, `state.py`).

They are different container apps. **Deploying only the api does not change indexing behaviour**, and basis/PnL is computed in the indexer. This has bitten people: an api-only deploy "fixing" a PnL bug fixes nothing.

Sibling repos that are *not* here (separate checkouts, separate deploys):
- `crystal interface` — the main frontend, deploys to **app.crystal.exchange** (not crystal.exchange). Pushes to `main` auto-deploy.
- `crystal-fun` — a separate frontend on its own domain. It is deliberately independent; do not "unify" it with the interface.
- `crystal contracts` / `crystal contracts-dev` — the solidity side.

### Two duplicate token-overview handlers

There are **two** token-overview route handlers: one in `api/routes/fun.py` and one in `api/routes/launchpad.py`. **crystal.fun is served by `fun.py`.** Editing `launchpad.py` alone and expecting crystal.fun to change is a well-trodden way to waste an hour.

---

## 2. Deploy

Deploys run through **GitHub Actions on push to `main`** (OIDC to Azure). Branch model is `dev` -> PR -> `main`.

**The production deploy sits behind a manual approval gate.** A successful deploy run can therefore show a wall-clock duration of *hours* (5h9m observed on 2026-09-02) while it waits for approval. A long-running "deploy" run is usually waiting, not broken. Check the actual container image before concluding a deploy did not happen:

```bash
az containerapp list -g crystal-prod-rg --query "[].{name:name,image:properties.template.containers[0].image,revision:properties.latestRevisionName,running:properties.runningStatus}" -o table
```

The image tag is the full commit sha, so you can diff it against `origin/main` directly. To check a specific fix is live: `git merge-base --is-ancestor <fix-sha> <deployed-sha>`.

Manual path, if you ever need it: `az acr build --registry crystalprodacr ...` then `az containerapp update ...` in resource group `crystal-prod-rg`. **If you build manually, build from an up-to-date checkout** — parallel sessions have twice redeployed from stale checkouts and silently reverted other people's fixes.

### 2026-09-02: api.crystal.exchange returns 522, and that is (currently) harmless

`api.crystal.exchange` sits behind Cloudflare and returns **522**. The origin is healthy — the Azure FQDN `crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io` serves 200.

Proven cause: `crystal-api` has `customDomains: null`, so Azure holds no certificate for `api.crystal.exchange` and drops the TLS handshake. Against origin IP `4.189.50.206`:
- SNI = the ACA FQDN -> **200**
- SNI = `api.crystal.exchange` -> **handshake fails**

So Cloudflare is reaching the origin without the Host/SNI override. Same failure as the 2026-08-21 incident, regressed.

**Impact today: none on the apps.** Neither frontend references `api.crystal.exchange`; both call the ACA FQDN directly. Fix is either Cloudflare-side (proxied CNAME to the ACA FQDN, or restore the Origin Rule overriding Host/SNI) or Azure-side (add the custom domain + managed cert, which needs a TXT validation record in Cloudflare). Both need Cloudflare dashboard access.

---

## 3. Database

**`crystal-prod-db-r3` is the only live server.** The older `crystal-prod-db` still accepts connections and still answers queries, but its data is stale. Pointing at it and drawing conclusions is a real trap that has burned time before — check your connection target before believing a surprising number.

Local dev uses a separate `crystal_dev` database; see `dev-env.ps1`. Keep prod isolated.

### The two lock rules — read these before writing any script that touches prod

Both were learned by taking the production API down, twice in one night. Both outages had the same shape: a long-held transaction parked an `ALTER TABLE`, and **once an `ACCESS EXCLUSIVE` request is waiting, Postgres queues every later reader behind it.** Symptom: plain selects start returning 500s while cached endpoints stay fine — that asymmetry is a good tell for lock contention rather than the app being down.

1. **Never hold a transaction open across slow work.** Read what you need, close the transaction, do the slow in-Python work, then open a short write transaction. A repair script once wrapped a multi-minute fold inside its read transaction (321s idle-in-transaction) and parked a migration behind it.
2. **Never fetch a huge result set in one read.** A single `SELECT` over 532k rows sat idle-in-transaction for 131s and parked a migration. Use keyset pagination with a few thousand rows per short transaction.

Also: **killing the python process does not close the Postgres backend.** After stopping any long script (including via a tool-level task stop), check `pg_stat_activity` for `idle in transaction` and `pg_terminate_backend` your orphan. One was observed holding a transaction for 203s after the process died.

A fast health check that has repeatedly been worth running first:

```sql
SELECT COUNT(*) FROM pg_locks WHERE NOT granted;
```

Nonzero means something is queued behind a lock. Zero means look elsewhere.

---

## 4. Conventions

- **No code comments. None, anywhere.** This is a standing instruction, not a style preference.
- Commit messages: lowercase, one sentence, descriptive of the change. **Never add Claude co-author trailers.**
- Commit and push freely.
- **Never `git add -A` in any crystal repo.** Multiple agent sessions share one working tree; `-A` will sweep up other people's in-flight edits. Stage explicit paths, and check `git status` for files you did not touch before committing.
