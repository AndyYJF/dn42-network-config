#!/usr/bin/env python3
"""Read-only audit of live OSPF and iBGP state using SSH keys or ssh-agent."""

import argparse
import json
import re
import subprocess
from pathlib import Path

from render import DEFAULT_INVENTORY, load_inventory, require_valid


MARKER = "__DN42_PROTOCOLS__"


def parse_ospf(text):
    counts = {"dn42_ospf": 0, "dn42_ospf6": 0}
    current = None
    for line in text.splitlines():
        match = re.match(r"^(dn42_ospf6?):\s*$", line.strip())
        if match:
            current = match.group(1)
            continue
        if current and "Full/" in line:
            counts[current] += 1
    return counts


def parse_ibgp(text):
    established = set()
    seen = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0].lower().startswith("dn42_ibgp_"):
            continue
        name = fields[0].lower()
        seen.add(name)
        if "Established" in fields:
            established.add(name)
    return seen, established


def evaluate_node(node_id, node, ospf_text, protocols_text):
    expected_peers = set(node["links"])
    ospf = parse_ospf(ospf_text)
    seen, established = parse_ibgp(protocols_text)
    expected_protocols = {link["protocol"].lower() for link in node["links"].values()}
    missing = sorted(expected_protocols - seen)
    down = sorted(expected_protocols - established)
    expected_count = len(expected_peers)
    ok = ospf == {"dn42_ospf": expected_count, "dn42_ospf6": expected_count} and not missing and not down
    return {
        "nodeId": node_id,
        "ok": ok,
        "ospf": ospf,
        "expectedNeighborsPerFamily": expected_count,
        "ibgp": {
            "expected": sorted(expected_protocols),
            "established": sorted(established & expected_protocols),
            "missing": missing,
            "down": down,
        },
    }


def ssh_command(host, user, identity, timeout):
    command = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=%s" % timeout,
    ]
    if identity:
        command.extend(["-i", str(Path(identity).expanduser())])
    command.extend([
        "%s@%s" % (user, host),
        "timeout 15 birdc show ospf neighbors; printf '\\n%s\\n'; timeout 15 birdc show protocols" % MARKER,
    ])
    return command


def audit_node(node_id, node, user="root", identity=None, timeout=10):
    result = subprocess.run(
        ssh_command(node["host"], user, identity, timeout),
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0:
        return {"nodeId": node_id, "ok": False, "error": (result.stderr or result.stdout).strip()[:500]}
    if MARKER not in result.stdout:
        return {"nodeId": node_id, "ok": False, "error": "audit marker missing from SSH output"}
    ospf_text, protocols_text = result.stdout.split(MARKER, 1)
    return evaluate_node(node_id, node, ospf_text, protocols_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    parser.add_argument("--node", action="append", help="audit only this node; may be repeated")
    parser.add_argument("--user", default="root")
    parser.add_argument("--identity", help="SSH private key; passwords are intentionally unsupported")
    parser.add_argument("--connect-timeout", type=int, default=10)
    args = parser.parse_args()

    inventory = load_inventory(args.inventory)
    require_valid(inventory)
    selected = args.node or sorted(inventory["nodes"])
    unknown = set(selected) - set(inventory["nodes"])
    if unknown:
        parser.error("unknown nodes: %s" % ", ".join(sorted(unknown)))
    results = [
        audit_node(node_id, inventory["nodes"][node_id], args.user, args.identity, args.connect_timeout)
        for node_id in selected
    ]
    summary = {"ok": all(result["ok"] for result in results), "results": results}
    print(json.dumps(summary, indent=2, sort_keys=True))
    raise SystemExit(0 if summary["ok"] else 1)


if __name__ == "__main__":
    main()
