#!/usr/bin/env python3
"""Guided installer for the access-node compose stack.

Single entry point for installing and configuring the stack (config file
init, compose.yaml generation, auto-update timer, start). Every step
derives its own done/available/locked state from the filesystem on each
run, so the wizard can be safely re-launched at any point (e.g. after the
user closes the terminal to edit config files). `update.sh` (invoked by
the systemd timer for periodic checks) calls back into this script via
`wizard.py generate`.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

EXAMPLE_SUFFIX = ".example"

COLOR = sys.stdout.isatty()
GREEN = "\033[92m" if COLOR else ""
RED = "\033[91m" if COLOR else ""
YELLOW = "\033[93m" if COLOR else ""
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


def dependencies_ok():
    """Silent check used to render the step's status in the menu."""
    return docker_cli_present() and docker_compose_plugin_present()


def status_tag(label, color):
    return f"{color}[{label}]{RESET}"


def run_dependency_check():
    """Verbose dependency check. Never exits — safe to run interactively from the menu."""
    print("Preflight checks:")
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

    return docker_ok and compose_ok


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


def backup_config_files(targets):
    timestamp = datetime.datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
    backup_path = ROOT / f"access-node-compose_config_{timestamp}.tgz"
    with tarfile.open(backup_path, "w:gz") as tar:
        for target in targets:
            tar.add(target, arcname=target.relative_to(ROOT))
    print(f"Backup created: {backup_path.relative_to(ROOT)}")
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


def read_environment():
    ensure_env_runtime()
    return parse_env_file(ENV_RUNTIME_FILE).get("DOME_ENVIRONMENT")


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


def resolve_versions_branch(env_vars):
    """Preview of the branch update.sh / step (3) would pull from: a local git
    read only, no fetch, so it can't guarantee the fetch itself will succeed."""
    explicit = env_vars.get("VERSIONS_BRANCH")
    if explicit:
        return explicit
    result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or "main"


def print_runtime_summary():
    if not ENV_RUNTIME_FILE.exists():
        return
    env_vars = parse_env_file(ENV_RUNTIME_FILE)
    branch = resolve_versions_branch(env_vars)
    branch_display = branch or "(not a git repo)"
    branch_color = GREEN if branch == "main" else YELLOW if branch else GRAY

    rows = [
        ("DOME_ENVIRONMENT", env_vars.get("DOME_ENVIRONMENT", "(not set)"), ""),
        ("COMPOSE_PROFILES", env_vars.get("COMPOSE_PROFILES", "(none)"), ""),
        ("VERSIONS_BRANCH", branch_display, branch_color),
    ]
    width = max(len(label) for label, _, _ in rows)

    print("Current runtime configuration (.env.runtime):")
    for label, value, color in rows:
        print(f"  {label:<{width}}  {color}{value}{RESET if color else ''}")
    print()


SERVICE_IMAGE_RE = re.compile(r"^  ([a-zA-Z0-9_.-]+):\s*$")


def generate_compose():
    """Render compose.yaml from compose-template.yaml + versions-<environment>.env."""
    environment = read_environment()
    if environment not in SUPPORTED_ENVIRONMENTS:
        print(
            f"Unsupported DOME_ENVIRONMENT='{environment}'. Use one of: {', '.join(SUPPORTED_ENVIRONMENTS)}.",
            file=sys.stderr,
        )
        sys.exit(1)

    versions_file = ROOT / f"versions-{environment}.env"
    if not versions_file.exists():
        print(f"Missing {versions_file.name} for environment '{environment}'.", file=sys.stderr)
        sys.exit(1)

    images = parse_env_file(versions_file)

    lines = (ROOT / "compose-template.yaml").read_text().splitlines()
    result = [f"# Generated for environment: {environment}"]
    current_service = None

    for line in lines:
        match = SERVICE_IMAGE_RE.match(line)
        if match:
            current_service = match.group(1)
            result.append(line)
            continue

        if current_service and line.strip().startswith("image:"):
            normalized = current_service.upper().replace("-", "_").replace(".", "_")
            image_value = images.get(f"{normalized}_IMAGE")
            result.append(f"    image: {image_value}" if image_value else line)
            current_service = None
            continue

        result.append(line)

    (ROOT / "compose.yaml").write_text("\n".join(result) + "\n")
    print(f"Generated: compose.yaml  (environment: {environment})")


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
  2. In .env.runtime, set DOME_ENVIRONMENT to one of: sbx, dev, dev2, prod.

Re-run this script when you're done: it will detect that automatically
and move on to the next step.
""")


# ---------------------------------------------------------------------------
# Step 3) pull versions + generate compose.yaml
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
    return template.replace("__WORKDIR__", str(ROOT))


def print_manual_systemd_instructions():
    print(f"""
Manual systemd timer installation:

  sudo cp systemd/{SERVICE_NAME}.tmpl {SYSTEMD_UNIT_DIR / SERVICE_NAME}
  sudo cp systemd/{TIMER_NAME}.tmpl {SYSTEMD_UNIT_DIR / TIMER_NAME}
  sudo sed -i "s|__WORKDIR__|{ROOT}|g" {SYSTEMD_UNIT_DIR / SERVICE_NAME}
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

def step_e_run():
    result = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "ps", "--all", "--format", "json"],
        capture_output=True,
        text=True,
        env=compose_env(),
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "Failed to query stack status.")
        return

    raw = result.stdout.strip()
    if not raw:
        print("No containers found for this stack (has it been started? see step 5).")
        return

    try:
        containers = json.loads(raw)
        if isinstance(containers, dict):
            containers = [containers]
    except json.JSONDecodeError:
        containers = [json.loads(line) for line in raw.splitlines() if line.strip()]

    containers.sort(key=lambda c: c.get("Service", "?"))
    name_width = max(len(c.get("Service", "?")) for c in containers)

    print(f"{'SERVICE':<{name_width}}  STATUS")
    for c in containers:
        service = c.get("Service", "?")
        state = c.get("State", "unknown")
        health = c.get("Health", "")
        label = "UP" if state == "running" else state.upper()
        suffix = f" ({health})" if health else ""
        print(f"{service:<{name_width}}  {label}{suffix}")


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

    print("These configuration files will be backed up:")
    for target in targets:
        print(f"  - {target.relative_to(ROOT)}")
    if not confirm("Create the backup now?"):
        print("Aborted: no backup created.")
        return

    backup_config_files(targets)


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
        title="Pull updated versions and generate compose.yaml",
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
    labels = [f"{s.key}, {s.alias}" for s in steps + commands] + ["generate"]
    width = max(len(label) for label in labels)

    lines = ["steps:"]
    for step in steps:
        lines.append(f"  {f'{step.key}, {step.alias}':<{width}}  {step.title}")

    lines.append("\nother commands:")
    for step in commands:
        lines.append(f"  {f'{step.key}, {step.alias}':<{width}}  {step.title}")

    lines.append(
        f"\n"
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
        choices=[c for s in STEPS for c in (s.key, s.alias)] + ["generate"],
        metavar="STEP",
        help="run a specific step directly (by letter or name), or 'generate', instead of the interactive menu (see steps below)",
    )
    args = parser.parse_args()

    if args.step == "generate":
        generate_compose()
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
