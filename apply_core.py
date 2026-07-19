#!/usr/bin/env python3
"""Transactionally apply repository-rendered OSPF/iBGP files on one node."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows can run unit tests; deployment remains Linux-only.
    fcntl = None


MANAGED_FILES = ("ospf.conf", "ibgp.conf")
MANAGED_DIRS = ("ospf", "ibgp")
REQUIRED_FILES = {"ospf.conf", "ospf/0.conf", "ibgp.conf"}
PROTOCOL_RE = re.compile(r"^\s*protocol\s+bgp\s+['\"]?([A-Za-z0-9_.=-]+)['\"]?\s+from\s+ibgpeers\b", re.M)


class ApplyError(RuntimeError):
    pass


def run(command, cwd=None, timeout=60):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _relative_files(root):
    return {
        str(path.relative_to(root)).replace("\\", "/"): path
        for path in Path(root).rglob("*") if path.is_file() or path.is_symlink()
    }


def validate_staging(staging_dir, expected_ibgp=3):
    staging = Path(staging_dir).resolve()
    if not staging.is_dir():
        raise ApplyError("staging directory does not exist: %s" % staging)
    files = _relative_files(staging)
    missing = REQUIRED_FILES - set(files)
    if missing:
        raise ApplyError("staging is missing required files: %s" % ", ".join(sorted(missing)))

    protocols = []
    ibgp_files = []
    for relative, path in files.items():
        parts = Path(relative).parts
        allowed = relative in MANAGED_FILES or (
            len(parts) == 2 and parts[0] in MANAGED_DIRS and relative.endswith(".conf")
        )
        if not allowed:
            raise ApplyError("staging contains an unmanaged path: %s" % relative)
        if path.is_symlink() or not path.is_file():
            raise ApplyError("staging files must be regular files: %s" % relative)
        if parts[0] == "ibgp":
            ibgp_files.append(relative)
            found = PROTOCOL_RE.findall(path.read_text(encoding="utf-8"))
            if len(found) != 1:
                raise ApplyError("%s must define exactly one iBGP protocol" % relative)
            protocols.extend(found)

    if len(ibgp_files) != expected_ibgp:
        raise ApplyError("expected %s iBGP files, found %s" % (expected_ibgp, len(ibgp_files)))
    if len(set(protocols)) != len(protocols):
        raise ApplyError("staging contains duplicate iBGP protocol names")
    return sorted(protocols)


def parse_ospf(text):
    counts = {"dn42_ospf": 0, "dn42_ospf6": 0}
    current = None
    for line in text.splitlines():
        match = re.match(r"^(dn42_ospf6?):\s*$", line.strip())
        if match:
            current = match.group(1)
        elif current and "Full/" in line:
            counts[current] += 1
    return counts


def parse_ibgp(text, expected_protocols):
    states = {}
    expected_lower = {name.lower(): name for name in expected_protocols}
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        key = fields[0].lower()
        if key in expected_lower:
            states[expected_lower[key]] = "Established" if "Established" in fields else (fields[-1] if len(fields) > 1 else "unknown")
    return {name: states.get(name, "missing") for name in expected_protocols}


def health_snapshot(expected_protocols, expected_neighbors, runner=run):
    ospf_result = runner(["birdc", "show", "ospf", "neighbors"], timeout=30)
    protocols_result = runner(["birdc", "show", "protocols"], timeout=30)
    if ospf_result.returncode != 0 or protocols_result.returncode != 0:
        raise ApplyError("birdc health query failed")
    ospf = parse_ospf(ospf_result.stdout)
    ibgp = parse_ibgp(protocols_result.stdout, expected_protocols)
    ok = (
        ospf == {"dn42_ospf": expected_neighbors, "dn42_ospf6": expected_neighbors}
        and all(state == "Established" for state in ibgp.values())
    )
    return {"ok": ok, "ospf": ospf, "ibgp": ibgp}


def wait_healthy(expected_protocols, expected_neighbors, timeout, runner=run, sleep=time.sleep):
    deadline = time.monotonic() + timeout
    last = None
    while True:
        last = health_snapshot(expected_protocols, expected_neighbors, runner)
        if last["ok"]:
            return last
        if time.monotonic() >= deadline:
            raise ApplyError("routing core did not recover before timeout: %s" % json.dumps(last, sort_keys=True))
        sleep(min(5, max(0, deadline - time.monotonic())))


def replace_managed(staging_dir, target_dir):
    staging = Path(staging_dir)
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    for name in MANAGED_FILES:
        shutil.copy2(staging / name, target / name)
        os.chmod(target / name, 0o644)
    for name in MANAGED_DIRS:
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(staging / name, destination)
        os.chmod(destination, 0o755)
        for path in destination.rglob("*"):
            if path.is_dir():
                os.chmod(path, 0o755)
            elif path.is_file():
                os.chmod(path, 0o644)


def backup_managed(bird_dir, backup_dir):
    bird = Path(bird_dir)
    backup = Path(backup_dir)
    backup.mkdir(parents=True, mode=0o700)
    existing = []
    for name in MANAGED_FILES + MANAGED_DIRS:
        source = bird / name
        if not source.exists():
            continue
        existing.append(name)
        destination = backup / name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    (backup / "backup.json").write_text(json.dumps({"existing": existing}, indent=2) + "\n", encoding="utf-8")
    return existing


def restore_managed(bird_dir, backup_dir):
    bird = Path(bird_dir)
    backup = Path(backup_dir)
    metadata = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    for name in MANAGED_FILES + MANAGED_DIRS:
        target = bird / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
    for name in metadata["existing"]:
        source = backup / name
        target = bird / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def parse_check(staging_dir, bird_dir, runner=run):
    temp_parent = "/var/tmp" if Path("/var/tmp").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="dn42-network-parse-", dir=temp_parent) as temporary:
        check_root = Path(temporary) / "bird"
        shutil.copytree(bird_dir, check_root)
        replace_managed(staging_dir, check_root)
        result = runner(["bird", "-p", "-c", "bird.conf"], cwd=check_root, timeout=60)
        if result.returncode != 0:
            raise ApplyError("BIRD parse check failed: %s" % (result.stderr or result.stdout).strip()[:1000])
        return (result.stderr or result.stdout).strip()


def bird_reconfigure(runner=run):
    result = runner(["birdc", "configure"], timeout=60)
    output = "%s\n%s" % (result.stdout, result.stderr)
    if result.returncode != 0 or not any(marker in output for marker in ("Reconfigured", "Reconfiguration in progress")):
        raise ApplyError("BIRD reconfigure failed: %s" % output.strip()[:1000])
    return output.strip()


def apply(staging_dir, bird_dir, backup_parent, expected_ibgp=3, recovery_timeout=300, dry_run=False, runner=run):
    protocols = validate_staging(staging_dir, expected_ibgp)
    pre_health = health_snapshot(protocols, expected_ibgp, runner)
    if not pre_health["ok"]:
        raise ApplyError("refusing deployment because the routing core is not healthy: %s" % json.dumps(pre_health, sort_keys=True))
    parse_warnings = parse_check(staging_dir, bird_dir, runner)
    if dry_run:
        return {"ok": True, "dryRun": True, "protocols": protocols, "preHealth": pre_health, "parseWarnings": parse_warnings}

    deploy_id = "%s-%s" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
    backup_dir = Path(backup_parent) / deploy_id
    backup_managed(bird_dir, backup_dir)
    promoted = False
    try:
        replace_managed(staging_dir, bird_dir)
        promoted = True
        configure_output = bird_reconfigure(runner)
        post_health = wait_healthy(protocols, expected_ibgp, recovery_timeout, runner)
        return {
            "ok": True,
            "dryRun": False,
            "backupDir": str(backup_dir),
            "protocols": protocols,
            "preHealth": pre_health,
            "postHealth": post_health,
            "configure": configure_output,
            "parseWarnings": parse_warnings,
        }
    except Exception as error:
        rollback_error = None
        if promoted:
            try:
                restore_managed(bird_dir, backup_dir)
                bird_reconfigure(runner)
                wait_healthy(protocols, expected_ibgp, recovery_timeout, runner)
            except Exception as caught:
                rollback_error = str(caught)
        detail = str(error)
        if rollback_error:
            detail += "; rollback failed: " + rollback_error
        else:
            detail += "; previous managed configuration restored"
        raise ApplyError(detail) from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", required=True)
    parser.add_argument("--bird-dir", default="/etc/bird")
    parser.add_argument("--backup-parent", default="/var/backups/dn42-network-core")
    parser.add_argument("--expected-ibgp", type=int, default=3)
    parser.add_argument("--recovery-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--lock-file", default="/run/lock/dn42-network-core.lock")
    args = parser.parse_args()

    if fcntl is None:
        raise SystemExit("apply_core.py requires Linux fcntl locking")

    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("another network-core deployment is running") from error
        try:
            summary = apply(
                args.staging_dir,
                args.bird_dir,
                args.backup_parent,
                args.expected_ibgp,
                args.recovery_timeout,
                args.dry_run,
            )
            print(json.dumps(summary, indent=2, sort_keys=True))
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}, indent=2), file=os.sys.stderr)
            raise SystemExit(1) from error


if __name__ == "__main__":
    main()
