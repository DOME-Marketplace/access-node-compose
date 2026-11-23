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
| `VERSION` | Release of the package (`X.Y.Z`, no `v`), shown by the wizard: installed nodes have no git, so this is how they know which release they run. |
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

**Changing the API URL.** Installed nodes keep `VERSIONS_API_URL` in their own `.env.runtime`: if the URL stops answering they stop updating (the running stack is untouched, but the timer logs a warning at every run). When a URL changes:

- keep the old URL answering for a transition period with an HTTPS **301** redirect to the new one: `fetch-versions` follows redirects between `https://` URLs, so installed nodes keep updating without any change on their side. Don't use 308: Python 3.8–3.10, which nodes may run, doesn't follow it;
- update the URLs in `.env.runtime.example` and publish a new release, with the change in the release notes;
- tell node operators to update `VERSIONS_API_URL` before the redirect is removed.

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

Users install from the **Source code (tar.gz)** archive that GitHub attaches to every Release: there is nothing to build or upload. GitHub creates it from the tagged commit and honors `.gitattributes`, whose `export-ignore` rules keep development files out of it (`.github/`, `.gitattributes`, `.gitignore` and this file). The archive extracts to `access-node-compose-X.Y.Z/` (GitHub drops the leading `v` of the tag); the README install and upgrade commands use `--strip-components=1` for that.

### Versioning

- Tags are `vX.Y.Z` ([Semantic Versioning](https://semver.org/)); `VERSION` holds the same number without the `v`.
- While on `0.x`, mark releases as **pre-release** on GitHub (only sbx has a versions API so far).
- Breaking changes for existing installations (renamed services, new mandatory settings, data migrations) must be described in the release notes and in the README upgrade section.
- Never move or delete a published tag: installations and links depend on it. Fix mistakes with a new patch release.

### Procedure

1. **Prepare the release on a branch**: set `VERSION` to `X.Y.Z` and check that the README upgrade section covers what changed since the previous release. Open the PR, wait for CI and approval, merge into `main`.
2. **Check the package** that GitHub will build, from the merged `main`:

   ```bash
   git fetch origin
   git archive --format=tar --prefix=access-node-compose-X.Y.Z/ origin/main | tar t | grep -v '/$'
   git show origin/main:VERSION
   ```

   Only user files must be listed (no `.github/`, `DEVELOPMENT.md`, `.gitattributes`, `.gitignore`), and `VERSION` must say `X.Y.Z`.
3. **Create the GitHub Release** from `https://github.com/DOME-Marketplace/access-node-compose/releases`, button **"Create a new release"** (**"Draft a new release"** once a release exists):
    - *Choose a tag*: type `vX.Y.Z`, then **"Create new tag: vX.Y.Z on publish"**; target `main`;
    - *Release title*: `vX.Y.Z`;
    - *Description*: the release notes (see below);
    - **Set as a pre-release** while on `0.x`;
    - **Publish release**. Don't upload any file: the archives are attached automatically. (The **Packages** section of the repository page is GitHub Packages, a registry for images and libraries: it is not used here.)
4. **Verify the published package** by installing it in an empty directory, following the README literally, up to wizard step 3 (don't start the stack on a host where one is already running: container names and ports are fixed):

   ```bash
   mkdir /tmp/release-test && cd /tmp/release-test
   curl -L https://github.com/DOME-Marketplace/access-node-compose/archive/refs/tags/vX.Y.Z.tar.gz \
     | tar xz --strip-components=1
   ./wizard.py --version     # must print X.Y.Z
   ./wizard.py 1 && ./wizard.py 2
   # set VERSIONS_API_URL in .env.runtime, then:
   ./wizard.py 3
   docker compose -f compose.yaml config -q
   ```

### Release notes

Written for the people installing or upgrading a node. Link the documentation **at the tag** (`.../blob/vX.Y.Z/README.md`), not at `main`, so readers see the instructions of that release. Sections:

- **Install**: the download and extract commands, and links to the README and `desmos.md`;
- **Highlights**: what changes for users, not for maintainers;
- **Upgrading**: what an existing installation must do by hand (or "nothing: the timer applies it");
- **Known limitations**.

## Conventions

- Code, comments, messages, documentation and commit messages in English.
- The compose file is `compose.yaml` (no `version:` field); configuration templates use the `.example` suffix.
- No new dependencies: `bash`, `python3` (standard library only) and `docker` on the user's machine.
