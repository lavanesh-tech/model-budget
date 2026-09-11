"""
Offline, no-Docker-daemon-required static verification for Step 39.

This does NOT build or run any image -- no Docker daemon was available
in the environment this was authored in (confirmed: `docker version`
reports "command not found" there). It checks the actual Dockerfile/
compose text for the concrete properties that matter (non-root user,
pinned base image, healthcheck present, no build-time secret ARGs, and
-- for compose.app.yaml -- that it actually parses as valid YAML with
the expected service topology). The REAL verification is the `docker
compose build` / `up` / healthcheck commands given alongside this
script; run those and paste their real output.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def check_dockerfile(path: Path, *, expected_user: str, expected_image_prefix: str) -> list[str]:
    problems = []
    if not path.exists():
        return [f"{path}: does not exist"]
    content = path.read_text()

    if f"USER {expected_user}" not in content:
        problems.append(f"{path}: missing 'USER {expected_user}' (must not run as root)")

    from_lines = [l for l in content.splitlines() if l.strip().upper().startswith("FROM ")]
    stage_names = {l.split()[-1] for l in from_lines if " AS " in l.upper()}
    external_from_lines = [l for l in from_lines if l.split()[1] not in stage_names]
    if not any(expected_image_prefix in l for l in external_from_lines):
        problems.append(f"{path}: no FROM line pins the expected base image ({expected_image_prefix})")
    for l in external_from_lines:
        image_ref = l.split()[1]
        if ":" not in image_ref or image_ref.endswith(":latest"):
            problems.append(f"{path}: unpinned or ':latest' base image: {image_ref}")

    if "HEALTHCHECK" not in content:
        problems.append(f"{path}: missing HEALTHCHECK instruction")

    arg_instructions = [l for l in content.splitlines() if l.strip().startswith("ARG ")]
    if arg_instructions:
        problems.append(f"{path}: contains real ARG instruction(s), a potential build-time secret surface: {arg_instructions}")

    return problems


def check_compose_app_yaml(path: Path) -> list[str]:
    problems = []
    if not path.exists():
        return [f"{path}: does not exist"]
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"{path}: invalid YAML: {exc}"]

    services = doc.get("services", {})
    if set(services.keys()) != {"gateway", "dashboard"}:
        problems.append(f"{path}: expected exactly services 'gateway' and 'dashboard', got {sorted(services.keys())}")
        return problems

    gw, dash = services["gateway"], services["dashboard"]

    if gw.get("depends_on", {}).get("db", {}).get("condition") != "service_healthy":
        problems.append(f"{path}: gateway must depend on db with condition service_healthy")
    if gw.get("depends_on", {}).get("redis", {}).get("condition") != "service_healthy":
        problems.append(f"{path}: gateway must depend on redis with condition service_healthy")
    if dash.get("depends_on", {}).get("gateway", {}).get("condition") != "service_healthy":
        problems.append(f"{path}: dashboard must depend on gateway with condition service_healthy")

    for svc_name, svc in (("gateway", gw), ("dashboard", dash)):
        for port_spec in svc.get("ports", []) or []:
            if not str(port_spec).startswith("127.0.0.1:"):
                problems.append(f"{path}: {svc_name} publishes {port_spec!r} -- must be bound to 127.0.0.1, not all interfaces")

    if dash.get("network_mode") != "service:gateway":
        problems.append(f"{path}: dashboard must use network_mode: service:gateway (see file's own comment for why)")
    if "ports" in dash:
        problems.append(f"{path}: dashboard must not declare its own ports under network_mode: service:gateway")

    backend_url = dash.get("environment", {}).get("BACKEND_ADMIN_BASE_URL", "")
    if "127.0.0.1" not in backend_url:
        problems.append(
            f"{path}: BACKEND_ADMIN_BASE_URL={backend_url!r} -- the real dashboard/lib/security.mjs "
            "backendRequest() only allows plaintext http:// to 127.0.0.1/localhost/[::1]"
        )

    return problems


def main() -> int:
    problems = []
    problems += check_dockerfile(ROOT / "Dockerfile", expected_user="modelbudget", expected_image_prefix="python:3.12.7-slim-bookworm")
    problems += check_dockerfile(ROOT / "dashboard" / "Dockerfile", expected_user="nextjs", expected_image_prefix="node:22.11.0-bookworm-slim")
    problems += check_compose_app_yaml(ROOT / "compose.app.yaml")

    if problems:
        print("PROBLEMS FOUND:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("Static checks passed:")
    print("  - Dockerfile: non-root USER, pinned base image, HEALTHCHECK present, no build-time secret ARGs")
    print("  - dashboard/Dockerfile: non-root USER, pinned base image, HEALTHCHECK present, no build-time secret ARGs")
    print("  - compose.app.yaml: valid YAML; gateway depends on db+redis (service_healthy); dashboard depends on "
          "gateway (service_healthy); both published ports bound to 127.0.0.1 only; dashboard correctly shares "
          "the gateway's network namespace and reaches it via the loopback hostname the real backend validator requires")
    print("\nThis does NOT confirm the images actually build or run -- no Docker daemon was available to verify "
          "that. Run the real `docker compose build`/`up` commands next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
