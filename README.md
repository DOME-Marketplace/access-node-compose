# Access Node Docker Compose: component and dependencies description

This README defines the formal requirements for the description of the Docker Compose file which implements a DOME Access Node using Docker Compose. The objective of this repo is to facilitate an Access Node implementation which is easy to understand, install and operate for those entities external to DOME which wish to use this form of Access Node.

The Docker Compose version of the Access Node is intended to be run externally to the DOME instance, so it should be self-contained and do not require any access to any service inside the DOME instance. The only communication between with the Access Node running in the DOME instance is for the replication protocol, implemented by the Desmos service.  

To ensure that all services remain integrated and documented, we use a metadata-as-code approach, including information about the services and their dependencies directly in the `compose.yaml` file.

Every service defined in this `compose.yaml` **must** include specific labels. These labels allow the generation of documentation and dependency graphs, which can be used to better implement safe and controlled deployments.

These labels are alligned with the [Backstage.io](https://backstage.io) standard, allowing us to use in the future that tool or any similar one to manage the software catalog in DOME.

---

## Implementation Example

When adding or updating your service, please follow this template:

```yaml
services:
  desmos:
    image: in2workspace/in2-desmos-api:v2.0.21
    # ... standard docker configs (ports, volumes, etc) ...
    labels:
      # Description
      - "dome.name=desmos"
      - "dome.description=Manages replication of data across nodes"

      # Ownership & Lifecycle
      - "dome.owner=Altia"
      - "dome.type=service"
      - "dome.lifecycle=sandbox"
            
      # Downstream Dependencies and version constraints
      - "dome.dependsOn=component:scorpio,component:auth-service"
      - "dome.versions=auth-service:>=2.1,scorpio:any"

```


## Service Metadata Standard

All architectural metadata must be prefixed with `dome.`, acting as our namespace. We will develop or use simple tooling to parse these labels to validate the architecture and render our **Service Graph**.

### Required Labels

| Label Key | Type | Description | Example |
| --- | --- | --- | --- |
| `dome.name` | String | The unique name of the service, corresponding to the service name in `compose.yml`. | `desmos` |
| `dome.description` | String | Short summary of the service's purpose. | `"Manages replication of data across nodes` |
| `dome.owner` | String | The company/team responsible for this service. | `Altia` |
| `dome.type` | Enum | The architectural role: `service`, `database`, `proxy`. | `service` |
| `dome.lifecycle` | Enum | Current maturity: `development`, `sandbox`, `preproduction`, `production`. | `production` |
| `dome.dependsOn` | List | Comma-separated list of services this component calls at runtime. | `component:scorpio,component:auth-service` |
| `dome.versions` | String | Version constraints for dependencies (CSV). | `auth-service:>=2.1,scorpio:any` |

---

## Additional information

1. **Unique Naming:** The service name in `compose.yaml` (e.g., `desmos`) is the "Entity Name." Use this exact name in `dome.dependsOn` prefixed with `component:`.
2. **No Comments for Metadata:** Do not use YAML comments (`#`) for owner or dependency info.
3. **Sync Required:** If you add a new API call from Service A to Service B, you **must** update the `dome.dependsOn` label for Service A in the same PR.
4. **CI Validation:** Our (WIP) CI pipeline runs a checker. If a service is missing the `dome.owner` or `dome.type` labels, the build will fail.


## Prerequisites

Before running the installation wizard, make sure the target machine has:

- **Docker Engine** and the **Docker Compose plugin** (`docker compose`, not the standalone `docker-compose`) — [install instructions](https://docs.docker.com/engine/install/).
- **Python 3.8+** — used to run `wizard.py` (standard library only, no extra packages to install).
- **git** — needed by `update.sh` to pull the latest `versions-<environment>.env` for periodic auto-updates; not required for a one-off manual install.
- **systemd** (optional) — only needed if you want the wizard to install the auto-update timer for you (step 4); without it you can still set up your own scheduling, or update manually.

`wizard.py` checks Docker and the Compose plugin automatically at startup (and on demand via step `1` of the menu) and stops with instructions if either is missing.

## Installation

The recommended way to install and configure the Access Node is the guided wizard:

<pre> ./wizard.py</pre>

It walks you through the following numbered steps, re-entrant at every run (safe to stop and relaunch at any point). Each step has both a number and a symbolic name — use whichever you prefer for direct/manual execution (e.g. `./wizard.py 5` and `./wizard.py start` are equivalent):

| Step | Name | Description |
| --- | --- | --- |
| `1` | `preflight` | Checks Docker and the Compose plugin are installed; runs automatically at every startup, but can also be re-run on demand (e.g. after fixing something). |
| `2` | `init` | Creates your working config files from the `.example` templates and tells you which ones to customize. |
| `3` | `generate-compose` | Pulls the latest known-good image versions for your `DOME_ENVIRONMENT` (set in `.env.runtime`: `sbx`, `dev`, `dev2`, or `prod`) and generates `compose.yaml` from `compose-template.yaml`. |
| `4` | `config-auto-update` | Offers to install and enable a systemd timer that periodically checks for new versions and restarts the stack (optional); if it can't install it automatically (no systemd, no sudo), it prints the manual steps. |
| `5` | `start` | Runs `docker compose pull` and `docker compose up -d`. |

The menu also lists a couple of lettered, non-sequential commands you can run at any point (they don't gate anything and are never auto-suggested as "the next step"):

| Command | Name | Description |
| --- | --- | --- |
| `a` | `status` | A friendlier, condensed alternative to `docker compose ps`: one line per service with its up/down state and health. |
| `b` | `backup` | Creates a timestamped `.tgz` backup of your current configuration files, on demand. |

Run `./wizard.py` again at any time to resume where you left off, or jump directly to a step/command with `./wizard.py <number-letter-or-name>` (e.g. `./wizard.py 3` or `./wizard.py generate-compose`). Run `./wizard.py -h` for the full list.

Step `3` (and `update.sh`'s automatic restarts) pull `versions-<environment>.env` from git. By default they use whichever branch is currently checked out on the machine; set `VERSIONS_BRANCH` in `.env.runtime` to pin a specific one (normally `main`). A yellow warning is printed whenever the branch in use isn't `main`, since that's not the intended production source.

`wizard.py` is the only script meant to be run by hand. The only other one you'll see in the repo, `update.sh`, isn't for manual use either — it's what the systemd timer set up in step `4` calls periodically to check for new versions and restart the stack.

### Logging

Set `LOGGING=true` and (optionally) `LOG_FILE` in `.env.runtime` (default: `./access-node-compose.log`) to keep a timestamped record of every step or command actually run — one line each, not the full command output:

```
[2026.07.07 12.42.30] step 3) Pull updated versions and generate compose.yaml
[2026.07.07 12.42.30] update.sh) regenerated compose.yaml for 'sbx' (dry run)
```

This applies both to manual wizard use and to `update.sh`'s automatic restarts via the systemd timer, so you get a single history of what ran and when. `update.sh` only adds a line when it actually finds and applies a new version — the periodic ticks where nothing changed stay silent, so the log doesn't fill up with no-ops.

## Configuration

To correctly configure the stack, you need to edit the following configuration files.

Please take a look at the provided examples and customize them for your own environment.

Inside every file, you will details for the parameters that you should customize for your environment.

- _**.env.desmos.example**_
- _**.env.dlt-adapter.example**_
- _**.secrets.desmos.example**_
- _**Caddyfile.example**_

Other files should not be changed

Please make a copy of the provided .example files before to customize them

| source file | destination file |
|-------------|------------------|
| .env.desmos.example | .env.desmos |
| .env.dlt-adapter-alastria.example | .env.dlt-adapter-alastria|
| .secrets.desmos.example | .secrets.desmos |
| .Caddyfile.example | .Caddyfile |

Once you have the working files, you can customize them.

Run `./wizard.py` to copy the working files from the examples for you (its first step) — see Installation above.

`compose.yaml` itself is generated, not edited by hand: it never contains hardcoded image versions, only variables filled in from `versions-<environment>.env` (`./wizard.py generate`, called by `wizard.py` step 3 and by `update.sh` for the periodic auto-update).


## Execution

The preferred way to start, restart, and check the Access Node is through the wizard (see Installation above):

<pre> ./wizard.py 5</pre>

This runs `docker compose pull` and `docker compose up -d` for you, automatically honoring any `COMPOSE_PROFILES` set in `.env.runtime` (see Dozzle below). To check the status of the running services afterwards:

<pre> ./wizard.py a</pre>

To stop the stack, use this command:

<pre> docker compose down</pre>

### Manual execution

If you prefer not to use the wizard, once `compose.yaml` has been generated (see Installation above) you can start the stack directly with:

<pre> docker compose up -d</pre>

Keep in mind that running `docker compose` directly does not pick up `COMPOSE_PROFILES` from `.env.runtime` — export it yourself first if needed (e.g. `export $(grep -v '^#' .env.runtime | xargs)`), or pass `--profile <name>` explicitly. This also means manual runs stay out of sync with what `update.sh`'s automatic restarts will do, since those always read `.env.runtime`.

### Dozzle
During setup/debug it is possible to start an instance of **Dozzle**, a service that provide a simple web UI to view container logs for all services, without having to view them from console.

Dozzle will NOT be started automatically unless enabled. The recommended way is to uncomment `COMPOSE_PROFILES=debug` in your `.env.runtime` (see `.env.runtime.example`): this applies consistently both to `./wizard.py` (steps 5/a) and to `update.sh`'s automatic restarts via the systemd timer, so Dozzle doesn't silently disappear on the next auto-update. For a one-off manual start without touching `.env.runtime`, you can still run

<pre> docker compose up -d --profile debug</pre>

Also, please note that with provided configuration, Dozzle will NOT be exposed via proxy in https, but it will only be reachable from exposed Dozzle port (i.e. 8888), usually from you private network (depending on your firewall rule).

Ex: http://dome.mydomain.com:8888/dozzle


If you want to enable proxy for Dozzle, please comment the related lines in Caddyfile

The service will be available from this URL: https://dome.mydomain.com/dozzle