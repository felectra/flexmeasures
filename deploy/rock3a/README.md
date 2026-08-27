# FlexMeasures pilot on Radxa ROCK 3A (rootless Podman)

Deployment recipe for running FlexMeasures **v1.0.0** as a trial on a Radxa ROCK 3A
(Rockchip RK3568, **aarch64**, 8 GB RAM) under **rootless Podman**, for internal access
over LAN / Tailscale, without SSL (`FLEXMEASURES_ENV=development`).

The stack is a single `podman-compose` project: `server` (gunicorn) + `worker`
(forecasting/scheduling/ingestion queues) + `db` (Postgres 17) + `queue` (Redis) +
`mailhog` (test mail).

> **Why we build the image locally:** the official `lfenergy/flexmeasures` image on Docker
> Hub is published for `amd64` only. There is no arm64 build to pull, so we build the
> image from source on the board (8 GB RAM handles it) — or, as a fallback, on an Apple
> Silicon Mac (native arm64) and transfer it with `podman save` / `podman load`.

---

## 0. Prerequisites (on the board, user `sd`)

Podman 5.x is already installed. Add a compose front-end and confirm tooling:

```bash
podman --version                      # expect 5.x
sudo apt-get update && sudo apt-get install -y podman-compose
podman-compose --version              # or: pipx install podman-compose
```

Allow the user's containers to keep running after logout and across reboots (needed for
rootless autostart, see step 7):

```bash
loginctl enable-linger sd
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
cd ~/flexmeasures
cp deploy/rock3a/.env.example deploy/rock3a/.env
```

Generate real values and put them into `deploy/rock3a/.env`:

```bash
python3 -c "import secrets; print('SECRET_KEY:', secrets.token_urlsafe())"
python3 -c "import secrets; print('TOTP hex:', secrets.token_hex(24))"
python3 -c "import secrets; print('a password:', secrets.token_hex(16))"
```

Edit `deploy/rock3a/.env`, replacing every `CHANGE_ME_*`. `SECURITY_TOTP_SECRETS` must stay
valid JSON, e.g. `{"1":"<the-hex-you-generated>"}`. The `.env` file is git-ignored — never
commit it.

## 3. Build the arm64 image

```bash
cd ~/flexmeasures
podman-compose -f deploy/rock3a/compose.pilot.yml build
```

The build downloads mostly prebuilt aarch64 wheels, so it should not compile much. On the
15 GB SD card, reclaim build cache afterwards:

```bash
podman image prune -f
df -h /
```

> **Fallback (build on an Apple Silicon Mac):**
> ```bash
> # on the Mac, in a checkout of this branch
> podman build --platform linux/arm64 -t localhost/flexmeasures-pilot:local .
> podman save localhost/flexmeasures-pilot:local | ssh sd@100.75.41.122 'podman load'
> ```
> Then skip the `build` step on the board and go straight to `up`.

## 4. Start the datastores, then the app

```bash
cd ~/flexmeasures
podman-compose -f deploy/rock3a/compose.pilot.yml up -d db queue mailhog
# give Postgres a few seconds to initialise, then:
podman-compose -f deploy/rock3a/compose.pilot.yml up -d server worker
```

The `server` container runs `flexmeasures db upgrade` on start (retrying until Postgres is
ready), then launches gunicorn.

## 5. One-time data initialisation

Populate standard structure and create a **real** account and admin user (no toy account):

```bash
FM="podman-compose -f deploy/rock3a/compose.pilot.yml exec server flexmeasures"

$FM add initial-structure
$FM add account --name "Felectra"           # note the printed account id (e.g. 1)
$FM add user --username admin --email admin@felectra.local --account-id 1 --roles admin
# ^ this prompts for the admin password
```

## 6. Verify

```bash
# health endpoint (from the board or any host on the Tailscale network)
curl -f http://localhost:5000/api/v3_0/health/ready && echo OK

# queue worker is registered and queues exist
podman-compose -f deploy/rock3a/compose.pilot.yml logs --tail=30 worker
```

Then open the UI at `http://<board-tailscale-ip>:5000` and log in with the admin user.
Captured e-mail (e.g. password resets) is visible at `http://<board-tailscale-ip>:8025`
(MailHog).

## 7. Autostart across reboots (rootless systemd)

`restart: unless-stopped` recovers crashes within a session, but rootless Podman has no
daemon to bring containers back after a reboot. Generate user systemd units and enable
them (linger was enabled in step 0):

```bash
cd ~/flexmeasures
mkdir -p ~/.config/systemd/user
# generate one unit per running container of this project:
podman generate systemd --new --files --name \
  $(podman ps --filter label=io.podman.compose.project=flexmeasures -q)
mv container-*.service ~/.config/systemd/user/ 2>/dev/null || true
systemctl --user daemon-reload
# enable the units that were generated (adjust names to what was produced):
systemctl --user enable --now container-flexmeasures_server_1.service
```

> Alternatively, model the stack as **Quadlet** `.container` / `.network` / `.volume` units
> under `~/.config/containers/systemd/` for a cleaner, declarative autostart. That is the
> recommended long-term shape and a good follow-up once the pilot is stable.

## 8. Operations

```bash
# logs
podman-compose -f deploy/rock3a/compose.pilot.yml logs -f server

# stop / start the whole stack
podman-compose -f deploy/rock3a/compose.pilot.yml down
podman-compose -f deploy/rock3a/compose.pilot.yml up -d

# back up the database volume
podman volume export fm-db-data --output ~/fm-db-backup-$(date +%F).tar

# keep the SD card healthy
podman image prune -f && df -h /
```

## Tuning notes

- **gunicorn** is set to `--workers 2 --threads 2`. Heavy computation runs in the `worker`
  container (via Redis queues), not in the web process, so the web tier stays light.
- **Disk is the tight resource** (15 GB SD). Watch `df -h /`; prune images and old build
  cache after rebuilds; consider moving Podman storage or the DB volume to an NVMe/USB SSD
  if the pilot grows.
- **No SSL by design** (internal pilot). If this later needs to be reachable outside the
  trusted network, switch `FLEXMEASURES_ENV` to `production` and put a reverse proxy
  (Caddy/nginx) with TLS in front — see `documentation/host/deployment.rst`.
