#!/usr/bin/env python3
"""Guided installer for the access-node compose stack.

Single entry point for installing and configuring the stack (config file
init, compose.yaml generation, auto-update timer, start). Every step
derives its own done/available/locked state from the filesystem on each
run, so the wizard can be safely re-launched at any point (e.g. after the
user closes the terminal to edit config files). `update.sh` (invoked by
the systemd timer for periodic checks) calls back into this script via
`wizard.py fetch-versions` and `wizard.py generate`.
"""

import argparse
import datetime
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

EXAMPLE_SUFFIX = ".example"

COLOR = sys.stdout.isatty()
GREEN = "\033[92m" if COLOR else ""
RED = "\033[91m" if COLOR else ""
YELLOW = "\033[93m" if COLOR else ""
ORANGE = "\033[38;5;208m" if COLOR else ""
GRAY = "\033[90m" if COLOR else ""
RESET = "\033[0m" if COLOR else ""


def docker_cli_present():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def docker_compose_plugin_present():
    try:
        subprocess.run(["docker", "compose", "version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def docker_daemon_reachable():
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


MIN_PYTHON = (3, 8)


def python_version_ok():
    return sys.version_info >= MIN_PYTHON


def dependencies_ok():
    """Silent check used to render the step's status in the menu."""
    return python_version_ok() and docker_cli_present() and docker_compose_plugin_present()


def status_tag(label, color):
    return f"{color}[{label}]{RESET}"


def run_dependency_check():
    """Verbose dependency check. Never exits — safe to run interactively from the menu."""
    print("Preflight checks:")
    python_ok = python_version_ok()
    required = ".".join(map(str, MIN_PYTHON))
    print(status_tag("ok", GREEN) if python_ok else status_tag("FAIL", RED), f"Python {sys.version.split()[0]}", end="")
    print("" if python_ok else f" — Python {required} or newer is required")

    docker_ok = docker_cli_present()
    print(status_tag("ok", GREEN) if docker_ok else status_tag("FAIL", RED), "Docker installed and on PATH", end="")
    print("" if docker_ok else " — install from https://docs.docker.com/engine/install/")

    compose_ok = docker_compose_plugin_present()
    print(status_tag("ok", GREEN) if compose_ok else status_tag("FAIL", RED), "'docker compose' plugin available", end="")
    print("" if compose_ok else " — install from https://docs.docker.com/compose/install/")

    if docker_ok and compose_ok:
        daemon_ok = docker_daemon_reachable()
        print(status_tag("ok", GREEN) if daemon_ok else status_tag("warn", YELLOW), "Docker daemon reachable", end="")
        print("" if daemon_ok else " (started? user permissions?) — needed for step (5)")

    return python_ok and docker_ok and compose_ok


def preflight_checks():
    """Fail fast at startup if Docker isn't usable, before the user invests time in config."""
    if not run_dependency_check():
        sys.exit(1)
    print()


def step_preflight_run():
    if not run_dependency_check():
        print("\nDependency check failed: fix the issues above before continuing.")


def customizable_targets():
    """Return (example_path, target_path) pairs for every *.example file."""
    pairs = []
    for example in sorted(ROOT.rglob(f"*{EXAMPLE_SUFFIX}")):
        if ".git" in example.parts:
            continue
        target = example.with_name(example.name[: -len(EXAMPLE_SUFFIX)])
        pairs.append((example, target))
    return pairs


def missing_targets():
    return [target for _, target in customizable_targets() if not target.exists()]


def init_config_files(force=False):
    """Copy every *.example file to its working target.

    If not force, existing targets are left untouched. If force, existing
    targets are overwritten with a fresh copy of the template.
    """
    for example, target in customizable_targets():
        existed = target.exists()
        if existed and not force:
            print(f"· Already exists: {target.relative_to(ROOT)} (skipped)")
            continue
        shutil.copy(example, target)
        print(f"{'Overwritten' if existed else 'Created'}: {target.relative_to(ROOT)}")


def create_missing_config_files():
    """Create the config files a newer release introduced (e.g. extracted over an
    existing installation) from their *.example, so the stack can still start.
    Existing files are never touched."""
    for example, target in customizable_targets():
        if not target.exists():
            shutil.copy(example, target)
            print(f"{YELLOW}Created {target.relative_to(ROOT)} from {example.name}: review its values.{RESET}")


PACKAGE_FILES = ("wizard.py", "update.sh", "compose-template.yaml", "systemd")
ENV_FILE_ITEM_RE = re.compile(r"^\s+-\s+(\S+)\s*$")
BIND_SOURCE_RE = re.compile(r"^\s+-\s+\"?\./([^:\"]+):")


def compose_local_files(compose_path):
    """Files compose.yaml reads from the project directory: env_file entries
    and ./ bind-mount sources (e.g. Caddyfile, postgres-init)."""
    files, in_env_file = set(), False
    for line in compose_path.read_text().splitlines():
        stripped = line.strip()
        if stripped == "env_file:":
            in_env_file = True
            continue
        if in_env_file:
            match = ENV_FILE_ITEM_RE.match(line)
            if match:
                files.add(match.group(1))
                continue
            if not stripped.startswith("#"):
                in_env_file = False
        match = BIND_SOURCE_RE.match(line)
        if match:
            files.add(match.group(1).rstrip("/"))
    return files


def full_backup_targets():
    """Everything needed to run the stack from another directory: compose.yaml,
    the versions file, every file it references, the user configuration, and
    the wizard/update tooling with its templates."""
    names = {"compose.yaml", ".env.runtime", *PACKAGE_FILES}
    names.update(str(t.relative_to(ROOT)) for _, t in customizable_targets())
    names.update(str(e.relative_to(ROOT)) for e, _ in customizable_targets())
    environment = parse_env_file(ENV_RUNTIME_FILE).get("DOME_ENVIRONMENT") if ENV_RUNTIME_FILE.exists() else None
    if environment in SUPPORTED_ENVIRONMENTS:
        names.add(versions_file_for(environment).name)
    if (ROOT / "compose.yaml").exists():
        names.update(compose_local_files(ROOT / "compose.yaml"))
    return sorted(ROOT / name for name in names if (ROOT / name).exists())


def backup_config_files(targets, kind="config"):
    """Write targets to access-node-compose_<kind>[_<env>]_<timestamp>.tgz,
    readable by the owner only: it contains secrets."""
    timestamp = datetime.datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    environment = parse_env_file(ENV_RUNTIME_FILE).get("DOME_ENVIRONMENT") if ENV_RUNTIME_FILE.exists() else None
    env_part = f"_{environment}" if environment in SUPPORTED_ENVIRONMENTS else ""
    backup_path = ROOT / f"access-node-compose_{kind}{env_part}_{timestamp}.tgz"
    fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tar:
        for target in targets:
            tar.add(target, arcname=target.relative_to(ROOT))
    print(f"Backup created: {backup_path.relative_to(ROOT)}  (permissions 600: it contains secrets)")
    return backup_path


ENV_RUNTIME_FILE = ROOT / ".env.runtime"
SUPPORTED_ENVIRONMENTS = ("sbx", "dev", "dev2", "prod")


def ensure_env_runtime():
    if ENV_RUNTIME_FILE.exists():
        return
    example = ROOT / ".env.runtime.example"
    if not example.exists():
        print("Missing .env.runtime. Copy .env.runtime.example to .env.runtime and set DOME_ENVIRONMENT.", file=sys.stderr)
        sys.exit(1)
    shutil.copy(example, ENV_RUNTIME_FILE)
    print(f"Created: {ENV_RUNTIME_FILE.name}")


def parse_env_file(path):
    values = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def compose_env():
    """Environment for docker compose subprocess calls: inherits the shell env,
    overlaid with .env.runtime (e.g. COMPOSE_PROFILES) so profile choices apply
    consistently to manual wizard runs and to update.sh's automatic restarts."""
    env = os.environ.copy()
    if ENV_RUNTIME_FILE.exists():
        env.update(parse_env_file(ENV_RUNTIME_FILE))
    return env


def log_action(message):
    """Append one timestamped line to LOG_FILE if LOGGING=true in .env.runtime.
    Just a record of what was run, not the command output itself."""
    if not ENV_RUNTIME_FILE.exists():
        return
    env_vars = parse_env_file(ENV_RUNTIME_FILE)
    if env_vars.get("LOGGING", "").strip().lower() != "true":
        return

    log_path = Path(env_vars.get("LOG_FILE") or "access-node-compose.log")
    if not log_path.is_absolute():
        log_path = ROOT / log_path

    timestamp = datetime.datetime.now().strftime("%Y.%m.%d %H.%M.%S")
    with log_path.open("a") as f:
        f.write(f"[{timestamp}] {message}\n")


def print_runtime_summary():
    if not ENV_RUNTIME_FILE.exists():
        return
    env_vars = parse_env_file(ENV_RUNTIME_FILE)
    api_url = env_vars.get("VERSIONS_API_URL")

    rows = [
        ("DOME_ENVIRONMENT", env_vars.get("DOME_ENVIRONMENT", "(not set)"), ""),
        ("COMPOSE_PROFILES", env_vars.get("COMPOSE_PROFILES", "(none)"), ""),
        ("VERSIONS_API_URL", api_url or "(not set, required)", "" if api_url else RED),
    ]
    width = max(len(label) for label, _, _ in rows)

    print("Current runtime configuration (.env.runtime):")
    for label, value, color in rows:
        print(f"  {label:<{width}}  {color}{value}{RESET if color else ''}")
    print()


# ---------------------------------------------------------------------------
# Image versions: fetched from the versions API, saved as
# versions-<environment>.env (never committed), rendered into compose.yaml
# ---------------------------------------------------------------------------

TEMPLATE_FILE = ROOT / "compose-template.yaml"
IMAGE_PLACEHOLDER = "__CONTAINER_IMAGE_PLACEHOLDER__"
SERVICE_IMAGE_RE = re.compile(r"^  ([a-zA-Z0-9_.-]+):\s*$")
IMAGE_KEY_RE = re.compile(r"^[A-Z0-9_]+_IMAGE$")
# [registry[:port]/]name[/name...][:tag][@sha256:digest]. Deliberately strict:
# values end up verbatim in compose.yaml, so nothing that could break the YAML
# or trigger compose interpolation (spaces, quotes, '$', '#') gets through.
IMAGE_REF_RE = re.compile(
    r"^(?:[a-zA-Z0-9.-]+(?::[0-9]+)?/)?"
    r"[a-z0-9]+(?:[._-]+[a-z0-9]+)*(?:/[a-z0-9]+(?:[._-]+[a-z0-9]+)*)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)
ETAG_RE = re.compile(r'^(?:W/)?"[\x21\x23-\x7e]*"$')
ETAG_COMMENT = "# etag: "
KEPT_COMMENT = "# kept-previous: "
MISSING_COMMENT = "# missing:"
CHART_COMMENT = "# chart:"
# Comment lines written by fetch-versions itself: never copied from an API response.
OWN_COMMENTS = ("# generated by wizard.py", "# source:", ETAG_COMMENT.strip(), "# fetched-at:", KEPT_COMMENT.strip())
MAX_RESPONSE_BYTES = 64 * 1024
DEFAULT_API_TIMEOUT = 30
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class VersionsApiError(Exception):
    pass


def require_environment(env_vars):
    environment = env_vars.get("DOME_ENVIRONMENT")
    if environment not in SUPPORTED_ENVIRONMENTS:
        print(
            f"Unsupported DOME_ENVIRONMENT='{environment}'. Use one of: {', '.join(SUPPORTED_ENVIRONMENTS)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return environment


def versions_file_for(environment):
    return ROOT / f"versions-{environment}.env"


def image_key(service):
    return service.upper().replace("-", "_").replace(".", "_") + "_IMAGE"


def template_image_services():
    """{service: key} (e.g. desmos: DESMOS_IMAGE) for the services whose image
    is a placeholder, in template order. Services with a static image (caddy,
    postgres, ...) are not listed: their version is pinned in the template itself."""
    services = {}
    current_service = None
    for line in TEMPLATE_FILE.read_text().splitlines():
        match = SERVICE_IMAGE_RE.match(line)
        if match:
            current_service = match.group(1)
            continue
        if current_service and line.strip().startswith("image:"):
            if IMAGE_PLACEHOLDER in line:
                services[current_service] = image_key(current_service)
            current_service = None
    return services


def template_image_keys():
    return list(template_image_services().values())


def read_versions_file(path):
    """Return ({KEY: image}, etag) from a versions file written by fetch-versions."""
    etag = None
    for line in path.read_text().splitlines():
        if line.startswith(ETAG_COMMENT):
            etag = line[len(ETAG_COMMENT):].strip() or None
    images = {k: v for k, v in parse_env_file(path).items() if IMAGE_KEY_RE.match(k)}
    return images, etag


def url_allowed(url):
    """https only; plain http is accepted just for local test servers."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS


def api_base_url(env_vars):
    url = env_vars.get("VERSIONS_API_URL", "").strip()
    if not url:
        print(
            "VERSIONS_API_URL is not set in .env.runtime: set it to the versions API "
            "of your DOME_ENVIRONMENT (the available ones are listed in .env.runtime.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    if not url_allowed(url):
        print(f"VERSIONS_API_URL must be an https:// URL, got '{url}'.", file=sys.stderr)
        sys.exit(1)
    return url.rstrip("/")


def api_timeout(env_vars):
    try:
        return max(1, int(env_vars.get("VERSIONS_API_TIMEOUT") or DEFAULT_API_TIMEOUT))
    except ValueError:
        return DEFAULT_API_TIMEOUT


class HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_allowed(newurl):
            raise VersionsApiError(f"refusing redirect to non-https URL {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_get(url, etag, timeout):
    """GET url, returning (status, body, etag). Raises VersionsApiError on any
    failure, including non-200 answers other than 304."""
    request = urllib.request.Request(url, headers={"User-Agent": "access-node-compose-wizard", "Accept": "text/plain"})
    if etag:
        request.add_header("If-None-Match", etag)
    opener = urllib.request.build_opener(HttpsOnlyRedirectHandler)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200:
                raise VersionsApiError(f"unexpected HTTP {response.status} from {url}")
            if len(body) > MAX_RESPONSE_BYTES:
                raise VersionsApiError(f"response from {url} is larger than {MAX_RESPONSE_BYTES} bytes")
            return 200, body, response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return 304, b"", etag
        raise VersionsApiError(f"HTTP {exc.code} from {url}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise VersionsApiError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}") from None


def parse_versions_response(body):
    """Parse the API body into ({KEY: image}, missing keys, chart, header comment lines)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise VersionsApiError("response is not valid UTF-8") from None

    images, missing, chart, header = {}, set(), None, []
    for number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if line.lower().startswith(OWN_COMMENTS):
                continue
            if line.startswith(MISSING_COMMENT):
                listed = (k.strip() for k in line[len(MISSING_COMMENT):].split(","))
                missing.update(k for k in listed if IMAGE_KEY_RE.match(k))
            elif line.startswith(CHART_COMMENT):
                chart = line[len(CHART_COMMENT):].strip()
            header.append(line)
            continue
        key, sep, value = line.partition("=")
        if not sep or not IMAGE_KEY_RE.match(key) or not IMAGE_REF_RE.match(value):
            raise VersionsApiError(f"invalid line {number} in response: {raw_line[:100]!r}")
        if key in images:
            raise VersionsApiError(f"duplicate key {key} in response")
        images[key] = value

    if not images:
        raise VersionsApiError("response contains no image versions")
    return images, missing, chart, header


def merge_with_previous(images, missing, previous):
    """Keep only the keys the template needs; services the API reports as
    missing keep their previous version. Returns ({KEY: image}, kept keys)."""
    expected = template_image_keys()

    unknown = sorted(set(images) - set(expected))
    if unknown:
        print(f"{YELLOW}Warning: ignoring keys not used by compose-template.yaml: {', '.join(unknown)}{RESET}")

    merged = {k: images[k] for k in expected if k in images}
    absent = [k for k in expected if k not in images]

    undeclared = [k for k in absent if k not in missing]
    if undeclared:
        raise VersionsApiError(f"response lacks {', '.join(undeclared)} without reporting them as missing")

    no_previous = [k for k in absent if k not in previous]
    if no_previous:
        raise VersionsApiError(
            f"the API reports {', '.join(no_previous)} as missing and there is no previous version to keep"
        )

    for key in absent:
        merged[key] = previous[key]
    if absent:
        print(f"{YELLOW}Warning: the API reports {', '.join(absent)} as missing: keeping the previous version.{RESET}")
    return merged, absent


def chart_matches(environment, chart):
    return re.search(rf"(?<![a-z0-9]){re.escape(environment)}(?![a-z0-9])", chart.lower()) is not None


def write_atomically(path, content):
    """Write via a temp file in the same directory + rename, so readers never
    see a half-written file and a failure leaves the old one untouched."""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def write_versions_file(path, url, etag, header, images, kept):
    lines = [
        "# Generated by wizard.py fetch-versions: do not edit, it is overwritten on every update.",
        f"# source: {url}",
    ]
    if etag and ETAG_RE.match(etag):
        lines.append(f"{ETAG_COMMENT}{etag}")
    lines.append(f"# fetched-at: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines += header
    if kept:
        lines.append(f"{KEPT_COMMENT}{','.join(kept)}")
    lines += [f"{key}={images[key]}" for key in sorted(images)]
    write_atomically(path, "\n".join(lines) + "\n")


def fetch_versions():
    """Refresh versions-<environment>.env from the versions API.

    Exit codes: 0 = file is up to date (new list saved, or HTTP 304);
    2 = API unreachable or answer rejected, the last good file is kept;
    1 = no usable versions file (configuration error, or first fetch failed).
    """
    ensure_env_runtime()
    env_vars = parse_env_file(ENV_RUNTIME_FILE)
    environment = require_environment(env_vars)
    url = f"{api_base_url(env_vars)}/versions"
    versions_file = versions_file_for(environment)

    previous, etag = read_versions_file(versions_file) if versions_file.exists() else ({}, None)

    try:
        status, body, new_etag = http_get(url, etag if previous else None, api_timeout(env_vars))
        if status == 304:
            print(f"Image versions unchanged for '{environment}' (HTTP 304).")
            return 0
        images, missing, chart, header = parse_versions_response(body)
        merged, kept = merge_with_previous(images, missing, previous)
    except VersionsApiError as exc:
        print(f"{YELLOW}Warning: could not update image versions: {exc}{RESET}", file=sys.stderr)
        if versions_file.exists():
            print(f"Keeping the last good {versions_file.name}.", file=sys.stderr)
            return 2
        print(f"No {versions_file.name} available yet: cannot continue.", file=sys.stderr)
        return 1

    if chart and not chart_matches(environment, chart):
        print(
            f"{YELLOW}Warning: the API serves chart '{chart}', which doesn't look like "
            f"DOME_ENVIRONMENT='{environment}': check VERSIONS_API_URL in .env.runtime.{RESET}"
        )

    write_versions_file(versions_file, url, new_etag, header, merged, kept)
    print(f"Updated: {versions_file.name}  (environment: {environment})")
    return 0


def generate_compose(output=None):
    """Render compose.yaml (or `output`) from compose-template.yaml + versions-<environment>.env.
    Nothing is written if any placeholder can't be resolved to a valid image."""
    ensure_env_runtime()
    environment = require_environment(parse_env_file(ENV_RUNTIME_FILE))
    create_missing_config_files()

    versions_file = versions_file_for(environment)
    if not versions_file.exists():
        print(
            f"Missing {versions_file.name}: run './wizard.py 3' (or './wizard.py fetch-versions') first.",
            file=sys.stderr,
        )
        sys.exit(1)

    images, _ = read_versions_file(versions_file)

    result = [f"# Generated for environment: {environment}"]
    current_service = None
    unresolved, invalid = [], []

    for line in TEMPLATE_FILE.read_text().splitlines():
        match = SERVICE_IMAGE_RE.match(line)
        if match:
            current_service = match.group(1)
        elif current_service and line.strip().startswith("image:"):
            if IMAGE_PLACEHOLDER in line:
                key = image_key(current_service)
                image = images.get(key)
                if not image:
                    unresolved.append(key)
                elif not IMAGE_REF_RE.match(image):
                    invalid.append(f"{key}={image}")
                else:
                    line = line.replace(IMAGE_PLACEHOLDER, image)
            current_service = None
        result.append(line)

    if unresolved or invalid:
        if unresolved:
            print(f"Missing in {versions_file.name}: {', '.join(unresolved)}", file=sys.stderr)
        if invalid:
            print(f"Invalid image references in {versions_file.name}: {', '.join(invalid)}", file=sys.stderr)
        print("compose.yaml was not generated.", file=sys.stderr)
        sys.exit(1)

    output_path = Path(output) if output else ROOT / "compose.yaml"
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    write_atomically(output_path, "\n".join(result) + "\n")
    print(f"Generated: {output_path.relative_to(ROOT)}  (environment: {environment})")


def run(cmd, env=None, check=True):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, env=env, check=check)


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{hint}] ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


class Step:
    def __init__(self, key, alias, title, is_done, run_action, precondition=None, blocked_hint="", category="step"):
        self.key = key
        self.alias = alias
        self.title = title
        self.is_done = is_done
        self.run_action = run_action
        self.precondition = precondition or (lambda: True)
        self.blocked_hint = blocked_hint
        self.category = category

    @property
    def available(self):
        return self.precondition()


# ---------------------------------------------------------------------------
# Step 2) init: create runtime config files from *.example
# ---------------------------------------------------------------------------

def step_a_done():
    return not missing_targets() and (ROOT / ".env.runtime").exists()


def step_a_run():
    targets = [t for _, t in customizable_targets()]
    existing = [t for t in targets if t.exists()]

    if existing:
        print("These configuration files already exist and would be overwritten with fresh templates:")
        for target in existing:
            print(f"  - {target.relative_to(ROOT)}")
        print()
        choice = input(
            "[b] back up then overwrite / [o] overwrite without backup / [C] cancel, do nothing: "
        ).strip().lower()
        if choice == "b":
            backup_config_files(existing)
        elif choice != "o":
            print("Cancelled: no files were changed.")
            return
        init_config_files(force=True)
    else:
        init_config_files()

    listing = "\n".join(f"  - {t.relative_to(ROOT)}" for t in targets)
    print(f"""
Configuration files are ready. Before moving on:

  1. Open and customize these files (each one has notes on the fields
     you need to fill in):
{listing}
  2. In .env.runtime, set DOME_ENVIRONMENT to one of: sbx, dev, dev2, prod,
     and VERSIONS_API_URL to the versions API of that environment.

Re-run this script when you're done: it will detect that automatically
and move on to the next step.
""")


# ---------------------------------------------------------------------------
# Step 3) fetch versions + generate compose.yaml
# ---------------------------------------------------------------------------

def step_b_done():
    return (ROOT / "compose.yaml").exists()


def step_b_run():
    env = os.environ.copy()
    env["DRY_RUN"] = "1"
    run([str(ROOT / "update.sh")], env=env)


SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
SERVICE_NAME = "access-node-update.service"
TIMER_NAME = "access-node-update.timer"


def systemctl_available():
    try:
        subprocess.run(["systemctl", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def step_c_done():
    if not systemctl_available():
        return False
    result = subprocess.run(["systemctl", "is-enabled", TIMER_NAME], capture_output=True, text=True)
    return result.stdout.strip() == "enabled"


def render_unit(name):
    template = (ROOT / "systemd" / f"{name}.tmpl").read_text()
    return template.replace("__WORKDIR__", str(ROOT)).replace("__USER__", getpass.getuser())


def print_manual_systemd_instructions():
    print(f"""
Manual systemd timer installation:

  sudo cp systemd/{SERVICE_NAME}.tmpl {SYSTEMD_UNIT_DIR / SERVICE_NAME}
  sudo cp systemd/{TIMER_NAME}.tmpl {SYSTEMD_UNIT_DIR / TIMER_NAME}
  sudo sed -i "s|__WORKDIR__|{ROOT}|g; s|__USER__|{getpass.getuser()}|g" {SYSTEMD_UNIT_DIR / SERVICE_NAME}
  sudo systemctl daemon-reload
  sudo systemctl enable --now {TIMER_NAME}

Then check its status with: systemctl status {TIMER_NAME}
""")


def step_c_run():
    if not systemctl_available():
        print("systemd is not available on this host: if your production server uses it, follow these instructions there:")
        print_manual_systemd_instructions()
        return

    if not confirm(
        "Should the wizard install and enable the auto-update timer now? (requires sudo)"
    ):
        print_manual_systemd_instructions()
        return

    try:
        service_content = render_unit(SERVICE_NAME)
        timer_content = render_unit(TIMER_NAME)
        for name, content in ((SERVICE_NAME, service_content), (TIMER_NAME, timer_content)):
            dest = SYSTEMD_UNIT_DIR / name
            subprocess.run(["sudo", "tee", str(dest)], input=content, text=True, capture_output=True, check=True)
        run(["sudo", "systemctl", "daemon-reload"])
        run(["sudo", "systemctl", "enable", "--now", TIMER_NAME])
        print(f"Timer {TIMER_NAME} installed and started.")
    except subprocess.CalledProcessError:
        print("Automatic installation failed (insufficient sudo permissions?). Proceed manually:")
        print_manual_systemd_instructions()


# ---------------------------------------------------------------------------
# Step 5) start the stack
# ---------------------------------------------------------------------------

def step_d_done():
    result = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "ps", "--status", "running", "-q"],
        capture_output=True,
        text=True,
        env=compose_env(),
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def step_d_run():
    if step_d_done():
        if not confirm(
            "The stack is already running — this will restart it and services will be briefly unavailable. Continue?",
            default=False,
        ):
            print("Aborted: the stack was not restarted.")
            return

    env = compose_env()
    run(["docker", "compose", "-f", "compose.yaml", "pull"], env=env)
    run(["docker", "compose", "-f", "compose.yaml", "up", "-d"], env=env)
    run(["docker", "compose", "-f", "compose.yaml", "ps"], env=env)


# ---------------------------------------------------------------------------
# Command a) check stack status
# ---------------------------------------------------------------------------

STATUS_API_TIMEOUT = 10


def image_tag(image):
    """Version part of an image reference: its tag, or its digest if untagged."""
    if not image:
        return "-"
    name, _, digest = image.partition("@")
    last_part = name.rsplit("/", 1)[-1]
    if ":" in last_part:
        return last_part.split(":", 1)[1]
    return digest[:19] if digest else "latest"


def versions_file_metadata(path):
    """Header comments of a versions file, e.g. {'gitops-commit': ..., 'fetched-at': ...}."""
    metadata = {}
    for line in path.read_text().splitlines():
        if line.startswith("# ") and ":" in line:
            key, value = line[2:].split(":", 1)
            metadata.setdefault(key.strip(), value.strip())
    return metadata


def latest_api_images(env_vars, local_images, local_etag):
    """Ask the versions API (read-only: nothing is saved) for the latest images.
    Returns ({KEY: image} or None, one-line note for the status header)."""
    base_url = env_vars.get("VERSIONS_API_URL", "").strip()
    if not base_url or not url_allowed(base_url):
        return None, f"{YELLOW}VERSIONS_API_URL not set or invalid: latest versions unknown{RESET}"
    url = f"{base_url.rstrip('/')}/versions"
    try:
        status, body, _ = http_get(url, local_etag if local_images else None, min(api_timeout(env_vars), STATUS_API_TIMEOUT))
        if status == 304:
            return dict(local_images), "the local versions file matches the versions API"
        images, _, _, _ = parse_versions_response(body)
    except VersionsApiError as exc:
        return None, f"{YELLOW}versions API not reachable ({exc}): showing local data only{RESET}"
    # Services the API reports as missing keep their local version, as update.sh does.
    merged = dict(local_images)
    merged.update(images)
    if merged != local_images:
        return merged, f"{YELLOW}the versions API has newer versions than the local versions file{RESET}"
    return merged, "the local versions file matches the versions API"


def compose_json(args):
    result = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", *args], capture_output=True, text=True, env=compose_env()
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"'docker compose {' '.join(args)}' failed.")
    raw = result.stdout.strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Older compose versions print one JSON object per line.
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


def step_e_run():
    try:
        containers = compose_json(["ps", "--all", "--format", "json"])
        configured = compose_json(["config", "--format", "json"]).get("services", {})
    except RuntimeError as exc:
        print(exc)
        return
    if isinstance(containers, dict):
        containers = [containers]
    if not containers:
        print("No containers found for this stack (has it been started? see step 5).")
        return

    env_vars = parse_env_file(ENV_RUNTIME_FILE)
    environment = env_vars.get("DOME_ENVIRONMENT", "?")
    versions_file = versions_file_for(environment)
    local_images, local_etag = read_versions_file(versions_file) if versions_file.exists() else ({}, None)

    if local_images:
        metadata = versions_file_metadata(versions_file)
        commit = metadata.get("gitops-commit", "?")[:7]
        print(f"Image versions: {environment}, gitops-commit {commit}, fetched {metadata.get('fetched-at', '?')}")
    else:
        print(f"Image versions: no {versions_file.name} yet (see step 3)")
    latest_images, api_note = latest_api_images(env_vars, local_images, local_etag)
    print(f"Versions API: {api_note}")
    print()

    service_keys = template_image_services()
    by_service = {c.get("Service", "?"): c for c in containers}
    services = sorted(set(by_service) | set(configured))

    rows, not_applied, new_on_api, unhealthy = [], 0, 0, 0
    for service in services:
        container = by_service.get(service)
        expected = configured.get(service, {}).get("image", "")
        latest = expected
        key = service_keys.get(service)
        if key and latest_images and latest_images.get(key):
            latest = latest_images[key]

        # Each cell is kept as (plain text, colored text): widths are computed on
        # the plain text, otherwise the color codes would break the alignment.
        if container:
            state = container.get("State", "unknown")
            health = container.get("Health", "")
            label = "UP" if state == "running" else state.upper()
            status = label + (f" ({health})" if health else "")
            status_colored = label if state == "running" else f"{RED}{label}{RESET}"
            if health:
                status_colored += f" ({RED}{health}{RESET})" if health == "unhealthy" else f" ({health})"
            if state != "running" or health == "unhealthy":
                unhealthy += 1
            running = container.get("Image", "")
        else:
            status, status_colored, running = "NOT CREATED", f"{RED}NOT CREATED{RESET}", ""
            unhealthy += 1

        pending = ""
        if latest and latest != running:
            pending = f"{ORANGE}-> {image_tag(latest)}{RESET}"
            if latest != expected:
                new_on_api += 1
                pending += " (new on API)"
            else:
                not_applied += 1
        rows.append((service, status, status_colored, image_tag(running) if running else "-", pending))

    name_width = max(len("SERVICE"), *(len(r[0]) for r in rows))
    status_width = max(len("STATUS"), *(len(r[1]) for r in rows))
    running_width = max(len("RUNNING"), *(len(r[3]) for r in rows))
    print(f"{'SERVICE':<{name_width}}  {'STATUS':<{status_width}}  {'RUNNING':<{running_width}}  PENDING")
    for service, status, status_colored, running, pending in rows:
        status_cell = status_colored + " " * (status_width - len(status))
        print(f"{service:<{name_width}}  {status_cell}  {running:<{running_width}}  {pending}".rstrip())

    print()
    if unhealthy:
        print(f"{RED}{unhealthy} service(s) unhealthy or not running: check them with 'docker logs <container>'.{RESET}")
    if not_applied:
        print(f"{ORANGE}{not_applied} service(s) not running the version in compose.yaml: run ./wizard.py 5 to apply it.{RESET}")
    if new_on_api:
        print(
            f"{ORANGE}{new_on_api} service(s) have newer versions on the versions API: run ./wizard.py 3, then "
            f"./wizard.py 5 (the auto-update timer of step 4 does both by itself).{RESET}"
        )
    if not not_applied and not new_on_api:
        print("All services run the latest available versions.")


# ---------------------------------------------------------------------------
# Command: back up configuration files on demand
# ---------------------------------------------------------------------------

def any_config_exists():
    return any(target.exists() for _, target in customizable_targets())


def step_backup_run():
    targets = [target for _, target in customizable_targets() if target.exists()]
    if not targets:
        print("No configuration files exist yet — nothing to back up (see step 2).")
        return

    print("""Backup type:
  [c] config: only your customized configuration files (.env.*, secrets, Caddyfile)
  [f] full:   everything needed to run the stack from another directory
              (config + compose.yaml, versions file, all files it references, wizard/update tooling)
Data volumes (databases, attachments) are never included.""")
    choice = input("[c] config / [f] full / [Q] cancel: ").strip().lower()
    if choice == "f":
        kind, targets = "full", full_backup_targets()
    elif choice == "c":
        kind = "config"
    else:
        print("Cancelled: no backup created.")
        return

    print(f"These files will be backed up ({kind}):")
    for target in targets:
        suffix = "/" if target.is_dir() else ""
        print(f"  - {target.relative_to(ROOT)}{suffix}")
    if not confirm("Create the backup now?"):
        print("Aborted: no backup created.")
        return

    backup_path = backup_config_files(targets, kind)
    if kind == "full":
        print(
            f"To run it elsewhere: mkdir <dir> && tar xzf {backup_path.name} -C <dir>, then from <dir> run "
            "./wizard.py 5.\nNote: container names and published ports are fixed, so it can't run on the "
            "same host while this stack is up."
        )


STEPS = [
    Step(
        key="1",
        alias="preflight",
        title="Check dependencies (Docker, docker compose plugin)",
        is_done=dependencies_ok,
        run_action=step_preflight_run,
    ),
    Step(
        key="2",
        alias="init",
        title="Initialize configuration files (from *.example)",
        is_done=step_a_done,
        run_action=step_a_run,
    ),
    Step(
        key="3",
        alias="generate-compose",
        title="Fetch image versions and generate compose.yaml",
        is_done=step_b_done,
        run_action=step_b_run,
        precondition=step_a_done,
        blocked_hint="requires step (2) completed",
    ),
    Step(
        key="4",
        alias="config-auto-update",
        title="Configure auto-update (systemd timer)",
        is_done=step_c_done,
        run_action=step_c_run,
        precondition=step_b_done,
        blocked_hint="requires step (3) completed",
    ),
    Step(
        key="5",
        alias="start",
        title="Start the stack",
        is_done=step_d_done,
        run_action=step_d_run,
        precondition=step_b_done,
        blocked_hint="requires step (3) completed",
    ),
    Step(
        key="a",
        alias="status",
        title="Check stack status",
        is_done=lambda: True,
        run_action=step_e_run,
        precondition=step_b_done,
        blocked_hint="requires step (3) completed",
        category="command",
    ),
    Step(
        key="b",
        alias="backup",
        title="Back up configuration files",
        is_done=lambda: True,
        run_action=step_backup_run,
        precondition=any_config_exists,
        blocked_hint="no configuration files to back up yet (see step 2)",
        category="command",
    ),
]


def find_step(value):
    return next((s for s in STEPS if value in (s.key, s.alias)), None)


def print_menu():
    print()
    print_runtime_summary()
    print("Access-node installation status:\n")
    for step in STEPS:
        if step.category != "step":
            continue
        mark = f"{GREEN}[x]{RESET}" if step.is_done() else "[ ]"
        line = f"  {mark} {step.key}) {step.title}"
        if not step.available:
            line += f"   ({step.blocked_hint})"
        print(line)

    commands = [s for s in STEPS if s.category == "command"]
    if commands:
        print("\nOther commands:\n")
        for step in commands:
            mark = f"{RESET}[-]{RESET}"
            # mark = f"{GRAY}[-]{RESET}"
            line = f"  {mark} {step.key}) {step.title}"
            if not step.available:
                line += f"   ({step.blocked_hint})"
            print(line)
    print()


def next_available_step():
    for step in STEPS:
        if step.category == "step" and step.available and not step.is_done():
            return step
    return None


def run_step(step):
    if not step.available:
        print(f"Step ({step.key}) is not available yet: {step.blocked_hint}.")
        return
    log_action(f"{step.category} {step.key}) {step.title}")
    try:
        step.run_action()
    except subprocess.CalledProcessError as exc:
        print(f"Step ({step.key}) failed (exit {exc.returncode}). Fix the error above and re-run the wizard.")
        sys.exit(exc.returncode)


def build_steps_epilog():
    steps = [s for s in STEPS if s.category == "step"]
    commands = [s for s in STEPS if s.category == "command"]
    labels = [f"{s.key}, {s.alias}" for s in steps + commands] + ["fetch-versions", "generate"]
    width = max(len(label) for label in labels)

    lines = ["steps:"]
    for step in steps:
        lines.append(f"  {f'{step.key}, {step.alias}':<{width}}  {step.title}")

    lines.append("\nother commands:")
    for step in commands:
        lines.append(f"  {f'{step.key}, {step.alias}':<{width}}  {step.title}")

    lines.append(
        f"\n"
        f"  {'fetch-versions':<{width}}  refresh versions-<environment>.env from VERSIONS_API_URL\n"
        f"  {'generate':<{width}}  (re)build compose.yaml from the local versions file\n"
        f"  {'':<{width}}  (no Docker required; used by CI and update.sh)"
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=build_steps_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "step",
        nargs="?",
        choices=[c for s in STEPS for c in (s.key, s.alias)] + ["fetch-versions", "generate"],
        metavar="STEP",
        help="run a specific step directly (by letter or name), or 'fetch-versions'/'generate', instead of the interactive menu (see steps below)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="with 'generate': write to FILE instead of compose.yaml",
    )
    args = parser.parse_args()

    if args.step == "fetch-versions":
        sys.exit(fetch_versions())

    if args.step == "generate":
        generate_compose(args.output)
        return

    step = find_step(args.step) if args.step else None

    if step is None or step.key != "1":
        preflight_checks()

    if step:
        run_step(step)
        return

    while True:
        print_menu()
        nxt = next_available_step()
        if nxt is None:
            print("All available steps are complete.")
            return
        choice = input(
            f"Press Enter to run the recommended next step ({nxt.key}), "
            f"type the letter/name of another step, or 'q' to quit: "
        ).strip().lower()
        if choice == "q":
            return
        if not choice:
            run_step(nxt)
            continue
        match = find_step(choice)
        if match is None:
            print("Invalid choice.")
            continue
        run_step(match)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted, exiting.")
        sys.exit(130)
