# FlexMeasures pilot on Radxa ROCK 3A (rootless Podman)

Deployment recipe for running FlexMeasures **v1.0.0** as a trial on a Radxa ROCK 3A
(Rockchip RK3568, **aarch64**, 8 GB RAM) under **rootless Podman**, for internal access
over LAN / Tailscale, without SSL (`FLEXMEASURES_ENV=development`).

The stack is a single `podman-compose` project: `server` (gunicorn) + `worker`
(forecasting/scheduling/ingestion queues) + `db` (Postgres 17) + `queue` (Redis) +
`mailhog` (test mail).

> **Why we build the image ourselves:** the official `lfenergy/flexmeasures` image on Docker
> Hub is published for `amd64` only — there is no arm64 build to pull. We build an arm64
> image on a separate arm64 host (an Apple Silicon Mac is ideal) and copy the finished image
> to the board with `podman save` / `podman load`, because the board's 14.5 GB eMMC is too
> small to run this build in place.

All `podman-compose` commands below are run **from this directory** (`deploy/rock3a/`) so
that the relative build context (`../..` = repo root) and the `.env` file resolve
correctly. Each session, set a short handle first:

```bash
cd ~/flexmeasures/deploy/rock3a
FMC="podman-compose --env-file .env -f compose.pilot.yml"
```

---

## 0. Host prerequisites (on the board, user `sd`)

**Rootless network backend.** Podman 5 needs a rootless network backend to give containers
connectivity (both during `build` and at runtime). The recommended backend is **pasta**
(package `passt`); without it, builds and runs fail with
`could not find pasta ... executable file not found`. Install it:

```bash
sudo apt-get update && sudo apt-get install -y passt
```

> Fallback without `passt`: if only `slirp4netns` is available, point Podman at it in
> `~/.config/containers/containers.conf`:
> ```ini
> [network]
> default_rootless_network_cmd = "slirp4netns"
> ```
> pasta is preferred (faster, the current default); use this only if you cannot install
> `passt`.

**Compose front-end and lingering.** `podman-compose` reads the `docker-compose`-style
file; lingering lets the rootless containers keep running after logout and start on boot
(see step 7):

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

Fill in real, generated values (Python 3 is preinstalled on Armbian). This one-liner writes
a complete `.env` with strong secrets without printing them:

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
]) + "\n")
env.chmod(0o600)
print("wrote", env.resolve())
PY
```

The `.env` file is git-ignored — never commit it, and do not run
`podman-compose ... config` (it prints resolved secrets). If a secret is ever exposed,
regenerate `.env` and recreate the containers.

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

The `server` container runs `flexmeasures db upgrade` on start (retrying until Postgres is
ready), then launches gunicorn. The `worker` waits until Postgres and Redis are reachable
before starting.

## 5. One-time data initialisation

Populate standard structure and create a **real** account and admin user (no toy account):

```bash
$FMC exec server flexmeasures add initial-structure
$FMC exec server flexmeasures add account --name "Felectra"        # note the printed id
$FMC exec server flexmeasures add user --username admin --email admin@felectra.local --account-id 1 --roles admin
# ^ prompts for the admin password
```

## 6. Verify

```bash
curl -f http://localhost:5000/api/v3_0/health/ready && echo OK
$FMC logs --tail=30 worker
```

Then open the UI at `http://<board-tailscale-ip>:5000` and log in with the admin user.
Captured e-mail (e.g. password resets) is visible at `http://<board-tailscale-ip>:8025`
(MailHog).

## 7. Autostart across reboots (rootless systemd)

`restart: unless-stopped` recovers crashes within a session, but rootless Podman has no
daemon to bring containers back after a reboot. Model the stack as **Quadlet** units (the
current, declarative approach) under `~/.config/containers/systemd/`, or generate units
from the running containers:

```bash
mkdir -p ~/.config/systemd/user
cd ~/.config/systemd/user
podman generate systemd --new --files --name \
  $(podman ps --filter label=io.podman.compose.project=flexmeasures -q)
systemctl --user daemon-reload
# enable each generated unit, e.g.:
systemctl --user enable --now container-flexmeasures_server_1.service
```

Lingering (enabled in step 0) is what lets these user units start at boot without an
interactive login.

## 8. Operations

```bash
$FMC logs -f server                 # follow logs
$FMC down                           # stop the stack
$FMC up -d                          # start the stack
podman volume export fm-db-data --output ~/fm-db-backup-$(date +%F).tar   # back up the DB
podman image prune -f && df -h /    # keep the SD card healthy
```

## Tuning notes

- **gunicorn** runs `--workers 2 --threads 4` (the upstream-vetted default). Heavy
  computation runs in the `worker` container via Redis queues, not in the web process.
- **Disk is the tight resource** (15 GB SD). Watch `df -h /`; prune images and build cache
  after rebuilds; consider moving Podman storage or the DB volume to an NVMe/USB SSD if the
  pilot grows.
- **No SSL by design** (internal pilot). If this later needs to be reachable outside the
  trusted network, switch `FLEXMEASURES_ENV` to `production` and put a reverse proxy
  (Caddy/nginx) with TLS in front — see `documentation/host/deployment.rst`.
