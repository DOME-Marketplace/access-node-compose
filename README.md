# DOME Access Node — Docker Compose

This package installs a **DOME Access Node** on your own server with Docker Compose. It is meant for organizations outside the DOME infrastructure that want to run their own node: the stack is self-contained, and its only link with the rest of DOME is the replication protocol, implemented by the Desmos service.

A guided installer, `wizard.py`, takes you from an empty server to a running node, and an optional systemd timer keeps the service versions up to date with the DOME environment you joined.

> Developing or maintaining this repository? See [DEVELOPMENT.md](https://github.com/DOME-Marketplace/access-node-compose/blob/main/DEVELOPMENT.md).

## What's in the stack

| Service | Role |
| --- | --- |
| `caddy` | HTTPS gateway: the single entry point from the Internet (port 443). Routes `/tmf-api/*` to the TM Forum APIs and `/desmos` to Desmos. |
| `tm-forum-api-*` (17 services) | TM Forum Open APIs (product catalog, party, ordering, …). |
| `desmos` | Replicates data with the other DOME Access Nodes, through the DLT. |
| `dlt-adapter-alastria` | Interface between Desmos and the Alastria blockchain. |
| `scorpio` | NGSI-LD Context Broker: where the TM Forum data is stored. |
| `postgres` | PostgreSQL/PostGIS database of Scorpio and Desmos. |
| `seaweedfs` | S3-compatible storage for document attachments (TM Forum Document Management). |
| `dozzle` | Optional web UI for container logs (only with the `debug` profile). |

## Requirements

**Server**

- Linux with **Docker Engine** and the **Docker Compose plugin** (`docker compose`, not the old `docker-compose`): [install instructions](https://docs.docker.com/engine/install/).
- **Python 3.8+**, standard library only (nothing to install with pip).
- **systemd** (optional), to let the wizard install the auto-update timer.
- Resources: the stack uses about **4 GB of RAM** when idle and its images take about **14 GB of disk**, plus your data. Plan for at least 8 GB of RAM.
- A user that can run `docker` without `sudo` (member of the `docker` group). `sudo` is only needed to install the systemd timer.

**Network**

- A **public DNS name** pointing to the server, with **port 443** reachable from the Internet: Caddy gets the TLS certificate automatically.
- Outbound HTTPS to: the versions API of your environment, the Docker registries (`docker.io`, `quay.io`), the Alastria RPC endpoint, GitHub (DOME trust framework lists) and the other DOME Access Nodes.

**DOME onboarding** (needed by Desmos and the DLT adapter, see [desmos.md](desmos.md))

- A **LEARCredentialMachine** issued to your organization.
- DLT keys generated with the DOME Access Node Key Generator.
- Your node registered in the DOME Trust Framework of your environment.

## Installation

### 1. Download

Download the latest release from the [Releases page](https://github.com/DOME-Marketplace/access-node-compose/releases) (`Source code (tar.gz)`) and extract it into the directory where the node will live, for example `/opt/access-node`:

```bash
sudo mkdir -p /opt/access-node && sudo chown "$USER" /opt/access-node
cd /opt/access-node
curl -L https://github.com/DOME-Marketplace/access-node-compose/archive/refs/tags/vX.Y.Z.tar.gz \
  | tar xz --strip-components=1
```

Replace `vX.Y.Z` with the release you want. Choose the directory carefully: the auto-update timer points to it, so don't move it after step 4.

### 2. Run the wizard

```bash
./wizard.py
```

The wizard shows the installation status and suggests the next step; press Enter to run it. You can stop at any time and relaunch it: it resumes where you left off. Each step also has a name, so `./wizard.py 3` and `./wizard.py generate-compose` are equivalent.

| Step | Name | What it does |
| --- | --- | --- |
| `1` | `preflight` | Checks Python, Docker and the Compose plugin. Runs automatically at every start. |
| `2` | `init` | Creates your configuration files from the `*.example` templates (see below). |
| `3` | `generate-compose` | Downloads the image versions of your environment and generates `compose.yaml`. Nothing is started or restarted. |
| `4` | `config-auto-update` | Installs the systemd timer that keeps the node up to date, running as your user (optional, needs `sudo`; prints the manual steps if it can't do it). |
| `5` | `start` | Pulls the images and starts the stack. |

### 3. Fill in the configuration (after step 2)

Step 2 creates these files; each one is commented, field by field.

| File | What to set |
| --- | --- |
| `.env.runtime` | `DOME_ENVIRONMENT` (`sbx`, `dev`, `dev2` or `prod`) and `VERSIONS_API_URL`, the versions API of that environment: the available URLs are listed in the file. |
| `Caddyfile` | Replace `:80` with your public domain name (no `https://`): Caddy then serves HTTPS with an automatic certificate. |
| `.env.desmos` | Desmos profile, your organization DID and public URL: see [desmos.md](desmos.md). |
| `.secrets.desmos` | Desmos private key and LEARCredentialMachine: see [desmos.md](desmos.md). |
| `.env.dlt-adapter-alastria` | DLT private key and ISS from the Key Generator: see [desmos.md](desmos.md). Leave `DOME_EVENTS_CONTRACT_ADDRESS` as it is: it is the shared DOME contract, not your DLT address. |
| `.env.s3` | Credentials of the attachment storage and of its admin UI: replace the `changeme-…` values. |

Do not edit the other configuration files (`.env.postgres`, `.env.scorpio`, `.env.tmforum-common`, `compose-template.yaml`): they are part of the package and are overwritten when you upgrade.

### 4. Generate, enable auto-update, start

Relaunch `./wizard.py` and run steps 3, 4 and 5. On the very first start, Scorpio prepares its database and the TM Forum services wait for it: allow a few minutes before everything is `healthy`.

### 5. Check

```bash
./wizard.py a                               # one line per service: state, health, version
curl https://<your-domain>/desmos/health    # from outside: Desmos through Caddy
```

## Settings in `.env.runtime`

| Variable | Required | Meaning |
| --- | --- | --- |
| `DOME_ENVIRONMENT` | yes | DOME environment this node belongs to: `sbx`, `dev`, `dev2` or `prod`. |
| `VERSIONS_API_URL` | yes | Versions API of that environment (base URL, up to `/v1`). |
| `VERSIONS_API_TIMEOUT` | no | Timeout in seconds for each call to the versions API (default 30). |
| `COMPOSE_PROFILES` | no | Set to `debug` to also run Dozzle. |
| `LOGGING`, `LOG_FILE` | no | `LOGGING=true` keeps a one-line record of every wizard step and of every update applied, in `LOG_FILE` (default `./access-node-compose.log`). |

## Day-to-day operations

| Task | Command |
| --- | --- |
| Status: state, health and version of every service, pending updates | `./wizard.py a` |
| Start or restart the stack | `./wizard.py 5` |
| Stop the stack | `docker compose down` |
| Logs of one service | `docker compose logs -f desmos` |
| Back up the configuration | `./wizard.py b` |
| Auto-update timer status / history | `systemctl status access-node-update.timer`, `journalctl -u access-node-update.service` |

**Status.** Besides state and health, `./wizard.py a` shows the version each service runs and flags pending updates in orange: versions already in `compose.yaml` but not applied yet (run step 5) and newer versions published by the versions API (run steps 3 and 5, or let the timer do it). Unhealthy or stopped services are shown in red.

**Backup.** `./wizard.py b` offers two kinds of backup, both written next to `wizard.py` with owner-only permissions (they contain secrets):

- `config`: your configuration files only (`access-node-compose_config_<env>_<timestamp>.tgz`);
- `full`: everything needed to run the stack from another directory, for example to debug it elsewhere (`access-node-compose_full_<env>_<timestamp>.tgz`).

Neither includes the **data** (databases, attachments): it lives in Docker volumes; back them up separately if you need to.

**Dozzle (log viewer).** Set `COMPOSE_PROFILES=debug` in `.env.runtime` and run `./wizard.py 5`: Dozzle is then reachable on port 8888 (`http://<server>:8888/dozzle`). It has no login: keep port 8888 closed to the Internet. To publish it through Caddy instead, uncomment the `/dozzle` block in your `Caddyfile`, aware that it becomes public.

**Manual `docker compose`.** It works, since `compose.yaml` is a plain Compose file, but it doesn't read `.env.runtime`: pass `--profile debug` yourself if you use Dozzle. Never edit `compose.yaml` by hand: it is regenerated at every update.

## How the node stays up to date

The versions of the DOME services are not part of this package. For each environment, DOME publishes them through a **versions API**; the infrastructure images (Caddy, PostgreSQL, SeaweedFS, Dozzle) are pinned in the package instead.

Every 15 minutes, the timer installed by step 4 runs `update.sh`, which:

1. asks the versions API for the current list (nothing is downloaded if it didn't change);
2. saves it as `versions-<environment>.env` and generates a new `compose.yaml`;
3. if something changed, pulls only the images that changed, then restarts what's needed.

It is built to run unattended. If the API doesn't answer or answers something invalid, the last good versions are kept and nothing is restarted. If an image can't be pulled, the running stack is left as it is. In both cases it simply retries at the next run.

## Upgrading to a new release

New releases change the installer and the stack definition (not the service versions, which update by themselves). Extract the new package over your installation: your configuration files are not part of the package, so they are kept.

```bash
cd /opt/access-node
./wizard.py b      # optional: back up your configuration first
curl -L https://github.com/DOME-Marketplace/access-node-compose/archive/refs/tags/vX.Y.Z.tar.gz \
  | tar xz --strip-components=1
```

The timer applies the new release at its next run, within 15 minutes; to apply it right away, run `./wizard.py 3` and then `./wizard.py 5`. Configuration files introduced by the new release are created with default values and named in a warning: review them, and compare `.env.runtime` with `.env.runtime.example`.

**From releases whose timer ran as root** (the timer unit has no `User=` line: check with `systemctl cat access-node-update.service`): run `./wizard.py 4` again to reinstall it with your user, and give back to your user the files root created: `sudo chown -R "$USER" /opt/access-node`.

**From releases that read versions from git** (`versions-<environment>.env` files in the package): add `VERSIONS_API_URL` to `.env.runtime`. Until then, updates stop with an error and the running stack is left untouched.

**From releases that used MinIO** for attachments: MinIO images are no longer available, so it was replaced by SeaweedFS (credentials in the new `.env.s3`). Attachments stored in MinIO are not migrated. Once you don't need them, remove the old volume with `docker volume rm <project>_minio-data` (`docker volume ls` shows the exact name).

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `VERSIONS_API_URL is not set` | Set it in `.env.runtime` (the URLs are listed in `.env.runtime.example`). |
| `Warning: could not update image versions: …` | The versions API is unreachable or answered with an error. The node keeps running with the last good versions; check the outbound connectivity to the API. Nothing to do if it's temporary. |
| `No versions-<env>.env available yet` | First installation with the versions API unreachable: the stack can't be generated until it answers. |
| A service is `unhealthy` or `EXITED` in `./wizard.py a` | Look at its logs: `docker compose logs <service>`. Right after a (re)start, Scorpio and the TM Forum services can take a few minutes. |
| Scorpio fails with `role "ngb" does not exist` | `.env.scorpio` comes from an old release: extract the current release again (see Upgrading). |
| HTTPS doesn't work | Check that the `Caddyfile` has your domain name instead of `:80`, that the DNS name points to the server and that port 443 is reachable; then `docker compose logs caddy`. |
| `Another update is already running` | The timer and a manual run overlapped: harmless, one of them simply skips. |
| Desmos sync errors | See the Troubleshooting section of [desmos.md](desmos.md). |

## Security notes

- Caddy is the entry point from the Internet, on port 443.
- **Scorpio (port 9090) and Dozzle (port 8888) are published on all network interfaces** on purpose: the node needs Scorpio reachable, and Dozzle is used for debugging. Neither has authentication, so allow them in your firewall only from the addresses that need them. SeaweedFS (8333, 12535) is published on `127.0.0.1` only.
- The configuration files contain secrets (private keys, credentials): keep them readable only by the user that runs the node, and keep backups (`*.tgz`) in a safe place.
- The admin UI of SeaweedFS is reachable from the server itself only, e.g. through an SSH tunnel: `ssh -L 12535:127.0.0.1:12535 <server>`, then `http://localhost:12535`.
