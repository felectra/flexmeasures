# labems-7f6.4 — read-only verification of the live FlexMeasures assets

Read-only observation of the FlexMeasures deployment on the Radxa ROCK 3A. **No writes**: only
`SELECT` queries and `podman`/`curl`/`git` inspection were run — no `INSERT/UPDATE/DELETE`, no
seeding, no EMS command, no physical-lab action. The initial population of the asset tree was
performed earlier under explicit owner authorization (the owner's instruction to create the
office assets) and is out of scope for this read-only record.

| Field | Value |
|---|---|
| Observed (UTC) | `2026-08-31T04:39:18Z` |
| Observer | BluePine (Claude, `claude-opus-4-8`) |
| Host | Rock 3A, Tailscale `100.75.41.122` |
| Deployment | rootless Podman compose (`deploy/rock3a/compose.pilot.yml`) |
| FlexMeasures version (running) | `0.1.dev1983+g9aa86e789.d20260827` → built from **`flexmeasures@9aa86e78`** |
| Image | `localhost/flexmeasures-pilot:local`, arch `arm64`, created `2026-08-27T08:27:17Z` |
| Deploy artifacts + asset seed | board checkout at **`5c5966de`** (`deploy/rock3a/seed-assets.py@5c5966de`) |
| DB schema (alembic) | `3bc1e29ca1f4` |
| Health | `GET /api/v3_0/health/ready` → `200` `{"database_sql":true,"database_redis":true}` |

## Containers (read-only)

```
rock3a_db_1       docker.io/library/postgres:17.11    Up 3 days (healthy)
rock3a_queue_1    docker.io/library/redis:7.4.11      Up 3 days
rock3a_mailpit_1  docker.io/axllent/mailpit:v1.31.0   Up 3 days
rock3a_worker_1   localhost/flexmeasures-pilot:local  Up 3 days
rock3a_server_1   localhost/flexmeasures-pilot:local  Up 3 days (healthy)
```

## Asset tree — account `Felectra` (id 1)

| id | name | type | parent | lat | lon |
|---|---|---|---|---|---|
| 5 | Office | building | root | 46.975 | 31.995 |
| 6 | BatteryBank | battery | 5 (Office) | 46.975 | 31.995 |
| 7 | String A | battery | 6 (BatteryBank) | 46.975 | 31.995 |
| 8 | String B | battery | 6 (BatteryBank) | 46.975 | 31.995 |
| 9 | Generator | process | root | 46.975 | 31.995 |

Sensors on these assets: **0** — sensor binding is a separate acceptance step, not in scope here.
The live tree matches `deploy/rock3a/seed-assets.py` and telemetry-v1 §3 exactly.

## Exact read-only commands

From a workstation on the tailnet, via `ssh -i ~/.ssh/id_ed25519_gitlab sd@100.75.41.122`:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
podman ps --filter name=rock3a_ --format '{{.Names}}  {{.Image}}  {{.Status}}'
podman exec rock3a_server_1 python -c 'import flexmeasures; print(flexmeasures.__version__)'
podman image inspect localhost/flexmeasures-pilot:local --format 'created={{.Created}} arch={{.Architecture}}'
git -C /home/sd/flexmeasures rev-parse HEAD
curl -s http://100.75.41.122:5000/api/v3_0/health/ready
podman exec -i rock3a_db_1 psql -U fm_pilot -d fm_pilot -c \
  "SELECT ga.id, ga.name, gat.name, ga.parent_asset_id, ga.latitude, ga.longitude \
   FROM generic_asset ga JOIN generic_asset_type gat ON gat.id = ga.generic_asset_type_id \
   WHERE ga.account_id = 1 ORDER BY ga.id;"
```

## Notes for the bead

- The live tree matches telemetry-v1 §3 (Office → BatteryBank → String A/B; Generator sibling).
- A stray experimental asset `office-n2.2Module` (parented under the public Battery Template)
  was removed during the earlier owner-authorized asset creation; the three public templates
  (Battery/EV/Heat Pump) remain as shipped by `flexmeasures add initial-structure`.
- The running **code** SHA (`9aa86e78`) predates the current branch tip; the **asset model +
  deploy recipe** are at `5c5966de`. Both are recorded above for traceability.
- This artifact is FlexMeasures-side evidence only. Recording/closing `labems-7f6.4` is done
  from the governing repository (`/data/projects/deye-imex/.beads`).
