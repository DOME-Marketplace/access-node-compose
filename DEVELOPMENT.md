# Development guide

This document is for whoever develops or maintains this repository. Installation and operation of a node are covered in [README.md](README.md), which is what users read; this file is not part of the release package.

## Repository layout

| Path | Purpose |
| --- | --- |
| `compose-template.yaml` | Stack definition. DOME service images are placeholders; infrastructure images are pinned here. |
| `wizard.py` | Guided installer and operations entry point (Python standard library only). Also provides the non-interactive commands used by `update.sh`: `fetch-versions` and `generate [--output FILE]`. |
| `update.sh` | Unattended update, run by the systemd timer and by wizard step 3 (`DRY_RUN=1`). |
| `systemd/*.tmpl` | Timer and service units installed by wizard step 4: `__WORKDIR__` becomes the installation directory and `__USER__` the user running the wizard, so updates never run as root. |
| `*.example` | Templates of the user configuration files, copied by wizard step 2. The real files are never committed. |
| `.env.postgres`, `.env.scorpio`, `.env.tmforum-common` | Fixed configuration shipped with the package (users must not edit them: upgrades overwrite them). |
| `Caddyfile.example` | Gateway routes; its upstream names must match the service names in the template. |
| `desmos.md` | User documentation for the Desmos/DLT onboarding. |
| `.github/` | CI workflows, the CI versions fixture and the tests. Not part of the release package. |

## How `compose.yaml` is built

`compose.yaml` is generated, never edited or committed:

```
versions API ──fetch-versions──▶ versions-<env>.env ──┐
                                                       ├──generate──▶ compose.yaml
compose-template.yaml ─────────────────────────────────┘
```

- In the template, a DOME service has `image: __CONTAINER_IMAGE_PLACEHOLDER__`. `generate` replaces it with the value of the key derived from the service name: upper case, `-` and `.` become `_`, plus `_IMAGE`. For example `tm-forum-api-account` becomes `TM_FORUM_API_ACCOUNT_IMAGE`.
- Infrastructure services (caddy, postgres, seaweedfs, dozzle) have no counterpart in the DOME GitOps repository: their image is pinned in the template, with a comment saying so.
- `generate` writes nothing if a placeholder can't be resolved, or if a value isn't a valid image reference (values end up verbatim in YAML: spaces, quotes, `$` and `#` are rejected).
- `generate` also creates missing user configuration files from their `*.example`, so a release that adds one doesn't break installations upgraded by extracting the package over the old one.

### The versions API

One instance per DOME environment reads the GitOps repository and serves:

- `GET <base>/versions`: `text/plain`, a few `#` comment lines (`chart`, `gitops-commit`, `generated-at`, and `# missing: KEY1,KEY2` for services not found in the chart), then sorted `KEY=value` lines with the same keys as the template. ETag, and `304` on `If-None-Match`; `503` until the first list is ready; never partial lists.
- `GET <base>/ready`: JSON with the state of the generator.

`<base>` is `VERSIONS_API_URL` in the user's `.env.runtime` (up to `/v1`), mandatory. Today only sbx has an instance; the URLs listed in `.env.runtime.example` for the other environments are placeholders.

`wizard.py fetch-versions` validates the answer before saving it as `versions-<env>.env`, with the ETag and the source in its header:

- a key listed in `# missing:` keeps its previous value, with a warning (error if there's no previous value);
- a key the template needs that is absent without being listed as missing makes the answer invalid;
- keys unknown to the template are ignored, with a warning;
- a `# chart:` that doesn't look like `DOME_ENVIRONMENT` only produces a warning.

Exit codes: `0` saved or unchanged (304); `2` API error or invalid answer, last good file kept; `1` no usable versions file (configuration error, or first fetch failed).

### `update.sh`

It runs unattended on third-party machines, so it must never leave a node worse than it found it:

1. `flock` on `.update.lock` (opened read-only: the timer may run as another user), so the timer and a manual run never overlap;
2. `fetch-versions`, bounded by `timeout`; on any API problem it exits without regenerating or restarting anything;
3. `generate --output compose.yaml.new`; if identical to `compose.yaml`, it stops there;
4. `docker compose config -q` on the candidate, then `pull` of **only the services whose image changed** (everything if the current `compose.yaml` can't be read): an unchanged image that is no longer downloadable can't block the other updates;
5. only if the pull succeeds: `mv` to `compose.yaml` and `up -d --remove-orphans`.

Every change to `update.sh` must be tested locally before opening a PR (see Tests).

## Service metadata labels

Every service in the template **must** carry `dome.*` labels: they document the services and their dependencies (metadata-as-code), aligned with the [Backstage.io](https://backstage.io) catalog model so that tool, or a similar one, can be used later.

| Label | Type | Description | Example |
| --- | --- | --- | --- |
| `dome.name` | String | Unique name, equal to the service name in the template. | `desmos` |
| `dome.description` | String | Short summary of the service's purpose. | `Manages replication of data across nodes` |
| `dome.owner` | String | Company or team responsible for the service. | `Altia` |
| `dome.type` | Enum | `service`, `database` or `proxy`. | `service` |
| `dome.lifecycle` | Enum | `development`, `sandbox`, `preproduction` or `production`. | `sandbox` |
| `dome.dependsOn` | List | Comma-separated services called at runtime, as `component:<name>`. | `component:scorpio,component:postgres` |
| `dome.versions` | String | Version constraints on the dependencies (CSV). | `postgres:any,scorpio:any` |

Rules:

1. Use the exact service name in `dome.dependsOn`, prefixed with `component:`.
2. Don't put owner or dependency information in YAML comments: use the labels.
3. If Service A starts calling Service B, update A's `dome.dependsOn` in the same PR.
4. CI fails if a service lacks one of the required labels (`.github/scripts/check_labels.py`).

## Adding or changing a service

- [ ] Add it to `compose-template.yaml` with all the `dome.*` labels and a healthcheck (the TM Forum services share the `x-tmf-healthcheck` anchor).
- [ ] DOME service: use the image placeholder, make sure the versions API exposes the matching `*_IMAGE` key, and add the key to `.github/ci/versions-ci.env` (CI checks that the fixture and the template have the same keys).
- [ ] Infrastructure service: pin a specific image tag, never `latest` (`update.sh` only pulls images whose reference changed).
- [ ] Exposed through the gateway: add the route to `Caddyfile.example`.
- [ ] New user configuration file: commit only its `*.example`, with commented, self-explanatory placeholder values, and add the real file to `.gitignore`.
- [ ] Publish ports on `127.0.0.1` unless they must be reachable from outside.

## Tests and CI

Run before every PR that touches `wizard.py` or `update.sh`:

```bash
python3 .github/scripts/test_update.py
```

It runs `update.sh` end to end in a throwaway copy of the package (no git, like a release), against a local mock of the versions API and a fake `docker` that records its arguments: no network and no Docker needed. It covers 200/304, 503, timeouts, malformed or oversized answers, missing and unknown keys, redirects, pull failures, selective pulls and concurrent runs.

The workflows in `.github/workflows/`:

- **CI - Docker Compose Validation**: generates `compose.yaml` from `.github/ci/versions-ci.env` (no access to the versions API), validates it with `docker compose config`, checks the labels and runs `test_update.py`. A separate non-blocking job fetches the real sbx versions API, to notice early if it stops matching the template.
- **CI - Docker Compose Up Test**: starts the stack with the same fixture.

## Releases

Users install from the `Source code (tar.gz)` archive of a GitHub Release. `.gitattributes` keeps development files out of it (`export-ignore`): `.github/`, `.gitattributes` and this file. The release procedure will be documented here.

## Conventions

- Code, comments, messages, documentation and commit messages in English.
- The compose file is `compose.yaml` (no `version:` field); configuration templates use the `.example` suffix.
- No new dependencies: `bash`, `python3` (standard library only) and `docker` on the user's machine.
