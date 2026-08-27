# FlexMeasures pilot on Radxa ROCK 3A (rootless Podman)

Deployment recipe for running FlexMeasures **v1.0.0** as a trial on a Radxa ROCK 3A
(Rockchip RK3568, **aarch64**, 8 GB RAM) under **rootless Podman**, reached internally over
LAN / Tailscale. It runs in **production** mode with forced HTTPS turned off
(`FLEXMEASURES_FORCE_HTTPS=False`) — so no TLS is required, but debugging and request
profiling stay off (unlike development mode).

The stack is a single `podman-compose` project: `server` (gunicorn) + `worker`
(forecasting/scheduling/ingestion queues) + `db` (Postgres 17) + `queue` (Redis) +
`mailhog` (test mail).

> **Why we build the image ourselves:** the official `lfenergy/flexmeasures` image on Docker
> Hub is published for `amd64` only — there is no arm64 build to pull. We build an arm64
> image on a separate arm64 host (an Apple Silicon Mac is ideal) and copy the finished image
> to the board with `podman save` / `podman load`, because the board's 14.5 GB eMMC is too
> small to run this build in place.

All `podman-compose` commands below are run **from this directory** (`deploy/rock3a/`) so
that the relative build context and the `.env` file resolve correctly. Each session, set a
short handle first:

```bash
cd ~/flexmeasures/deploy/rock3a
FMC="podman-compose --env-file .env -f compose.pilot.yml"
```

---

## 0. Host prerequisites (on the board, user `sd`)

**Rootless network backend.** Podman 5 needs a rootless network backend to give containers
connectivity. The recommended backend is **pasta** (package `passt`); without it, runs fail
with `could not find pasta ... executable file not found`:

```bash
sudo apt-get update && sudo apt-get install -y passt
```

> Fallback without `passt`: if only `slirp4netns` is available, point Podman at it in
> `~/.config/containers/containers.conf`:
> ```ini
> [network]
> default_rootless_network_cmd = "slirp4netns"
> ```

**Compose front-end and lingering.** Lingering lets the rootless containers keep running
after logout and start on boot (see step 7):

```bash
sudo apt-get install -y podman-compose
sudo loginctl enable-linger sd
podman --version && podman-compose --version
```

## 1. Get the code onto the board

```bash
cd ~
git clone https://github.com/felectra/flexmeasures.git
cd flexmeasures
git checkout pilot/rock3a-podman
```

## 2. Create the secrets file

```bash
cd ~/flexmeasures/deploy/rock3a
cp .env.example .env
chmod 600 .env
```

Fill in real values. This writes a complete `.env` with strong secrets without printing
them, then sets the network exposure (adjust `BIND_ADDR`/`TRUSTED_HOSTS` to your board):

```bash
python3 - <<'PY'
import secrets, pathlib
env = pathlib.Path(".env")
env.write_text("\n".join([
    "POSTGRES_DB=fm_pilot",
    "POSTGRES_USER=fm_pilot",
    "POSTGRES_PASSWORD=" + secrets.token_hex(16),
    "FLEXMEASURES_REDIS_PASSWORD=" + secrets.token_hex(16),
    "SECRET_KEY=" + secrets.token_urlsafe(48),
    'SECURITY_TOTP_SECRETS={"1":"' + secrets.token_hex(24) + '"}',
    "BIND_ADDR=100.75.41.122",
    "TRUSTED_HOSTS=127.0.0.1,localhost,100.75.41.122",
]) + "\n")
env.chmod(0o600)
print("wrote", env.resolve())
PY
```

`BIND_ADDR` publishes the UI/API and MailHog only on that interface (the board's Tailscale
IP), not on `0.0.0.0`. `TRUSTED_HOSTS` must contain every address you reach the server by —
in production, a Host header that is not listed is rejected. The `.env` file is git-ignored
— never commit it, and do not run `podman-compose ... config` (it prints resolved secrets).
If a secret is ever exposed, regenerate `.env` and recreate the containers.

## 3. Build the arm64 image on a separate arm64 host

The board's 14.5 GB eMMC is too small to build this image in place — the build peaks well
above the free space. Build it on any arm64 machine with room (an Apple Silicon Mac is
ideal — native arm64) and copy the finished image (~1–1.5 GB) to the board.

On the build host, with this branch checked out at the repo root:

```bash
podman build --platform linux/arm64 -t localhost/flexmeasures-pilot:local .
podman save localhost/flexmeasures-pilot:local | ssh sd@100.75.41.122 'podman load'
```

`podman load` on the board is rootless — no sudo. Confirm the image arrived:

```bash
podman images | grep flexmeasures-pilot
```

> If the build host cannot reach the board directly, save to a file and copy it over
> instead: `podman save -o fm-image.tar localhost/flexmeasures-pilot:local`, transfer
> `fm-image.tar`, then `podman load -i fm-image.tar` on the board.

## 4. Start the datastores, then the app

```bash
$FMC up -d db queue mailhog
$FMC up -d server worker
```

The `server` container waits until Postgres accepts connections, runs `flexmeasures db
upgrade` once (a real migration error stops the container with a visible reason, rather than
looping silently), then launches gunicorn. The `worker` waits until Postgres and Redis are
reachable before starting.

## 5. One-time data initialisation

Populate standard structure and create a **real** account and admin user (no toy account):

```bash
$FMC exec server flexmeasures add initial-structure
$FMC exec server flexmeasures add account --name "Felectra"
```

Note the account id this prints, then use it (it is `1` on a fresh database, but read the
printed value — a restored database may differ):

```bash
$FMC exec server flexmeasures add user --username admin --email admin@felectra.local --account-id <printed-id> --roles admin
# ^ prompts for the admin password
```

## 6. Verify

```bash
curl -f http://100.75.41.122:5000/api/v3_0/health/ready && echo OK
$FMC logs --tail=30 worker
```

Then open the UI at `http://100.75.41.122:5000` and log in with the admin user. Captured
e-mail (e.g. password resets) is visible at `http://100.75.41.122:8025` (MailHog).

## 7. Autostart across reboots (rootless systemd)

`restart: unless-stopped` recovers crashes within a session, but rootless Podman has no
daemon to restart the stack after a reboot. Run the whole compose project as one systemd
**user** service. It reads secrets from the mode-0600 `.env`, so nothing sensitive is baked
into a unit file. Lingering was enabled in step 0.

First stop the manually-started stack, then create the unit and let systemd own it:

```bash
$FMC down
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/flexmeasures-pilot.service <<'UNIT'
[Unit]
Description=FlexMeasures pilot (podman-compose)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/flexmeasures/deploy/rock3a
ExecStart=/usr/bin/podman-compose --env-file .env -f compose.pilot.yml up
ExecStop=/usr/bin/podman-compose --env-file .env -f compose.pilot.yml down
Restart=on-failure
TimeoutStopSec=90

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now flexmeasures-pilot.service
systemctl --user status flexmeasures-pilot.service
```

Reboot once and confirm the stack comes back with `podman ps`.

> For a more granular, per-container setup you can model the stack as **Quadlet** units under
> `~/.config/containers/systemd/`, each referencing the same `.env` as an `EnvironmentFile`.
> Do **not** use `podman generate systemd --new` here: it serialises the resolved environment
> (database/Redis passwords, `SECRET_KEY`, TOTP secrets) into the unit files.

## 8. Operations

```bash
$FMC logs -f server                 # follow logs
$FMC down                           # stop the stack   (or: systemctl --user stop flexmeasures-pilot)
$FMC up -d                          # start the stack  (or: systemctl --user start flexmeasures-pilot)

# Consistent online database backup with pg_dump (a live volume copy is NOT a valid backup):
$FMC exec -T db pg_dump -U fm_pilot fm_pilot | gzip > ~/fm-db-backup-$(date +%F).sql.gz

podman image prune -f && df -h /    # keep the eMMC healthy
```

## Tuning notes

- **gunicorn** runs `--workers 2 --threads 4` (the upstream-vetted default). Heavy
  computation runs in the `worker` container via Redis queues, not in the web process.
- **No TLS by design** (internal pilot over Tailscale): production mode with
  `FLEXMEASURES_FORCE_HTTPS=False`. If this later needs to be reachable outside the trusted
  network, enable real HTTPS — terminate TLS with `tailscale serve`, or put a reverse proxy
  (Caddy/nginx) in front — and drop the `FORCE_HTTPS=False` override. See
  `documentation/host/deployment.rst`.
- **Two-factor auth** is off (`SECURITY_TWO_FACTOR` unset). For a longer-lived deployment,
  consider enabling it — see `documentation/host/installation.rst`.
- **Disk** (15 GB eMMC): watch `df -h /`; prune images after image updates; consider an
  NVMe/USB SSD for Podman storage or the DB volume if the pilot grows.
