#!/usr/bin/env python3
"""Render and stage one node's core through SSH keys; apply requires confirmation."""

import argparse
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from render import DEFAULT_INVENTORY, load_inventory, render, require_valid


ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "apply_core.py"


def ssh_options(identity=None, timeout=10):
    options = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=%s" % timeout,
    ]
    if identity:
        options.extend(["-i", str(Path(identity).expanduser())])
    return options


def execute(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    parser.add_argument("--node", required=True)
    parser.add_argument("--mode", choices=("local", "remote-check", "apply"), default="local")
    parser.add_argument("--confirm-node", help="must exactly match --node when --mode apply")
    parser.add_argument("--user", default="root")
    parser.add_argument("--identity", help="SSH private key; passwords are intentionally unsupported")
    parser.add_argument("--connect-timeout", type=int, default=10)
    args = parser.parse_args()

    inventory = load_inventory(args.inventory)
    require_valid(inventory)
    if args.node not in inventory["nodes"]:
        parser.error("unknown node: %s" % args.node)
    if args.mode == "apply" and args.confirm_node != args.node:
        parser.error("--mode apply requires --confirm-node %s" % args.node)

    with tempfile.TemporaryDirectory(prefix="dn42-network-deploy-") as temporary:
        output = Path(temporary) / "rendered"
        manifest = render(inventory, output)
        node_dir = output / args.node
        if args.mode == "local":
            print(json.dumps({"ok": True, "mode": "local", "nodeId": args.node, "files": manifest["nodes"][args.node]["files"]}, indent=2))
            return

        node = inventory["nodes"][args.node]
        destination = "%s@%s" % (args.user, node["host"])
        deploy_id = uuid.uuid4().hex[:10]
        remote_dir = "/var/tmp/dn42-network-core-%s-%s" % (args.node, deploy_id)
        remote_helper = remote_dir + "-apply.py"
        options = ssh_options(args.identity, args.connect_timeout)
        execute(["scp", "-r", *options, str(node_dir), "%s:%s" % (destination, remote_dir)])
        execute(["scp", *options, str(HELPER), "%s:%s" % (destination, remote_helper)])
        remote_command = [
            "python3", remote_helper,
            "--staging-dir", remote_dir,
            "--expected-ibgp", str(len(node["links"])),
            "--recovery-timeout", "300",
        ]
        if args.mode == "remote-check":
            remote_command.append("--dry-run")
        output_text = execute(["ssh", *options, destination, " ".join(remote_command)])
        print(output_text)


if __name__ == "__main__":
    main()
