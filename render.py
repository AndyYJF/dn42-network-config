#!/usr/bin/env python3
"""Render the repository-owned BIRD core without touching dynamic peers."""

import argparse
import hashlib
import ipaddress
import json
import re
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_INVENTORY = ROOT / "inventory.json"
DEFAULT_OUTPUT = ROOT / ".rendered"
TEMPLATES = ROOT / "templates"
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9_.=-]{1,15}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.=-]{1,64}$")
FORBIDDEN_KEYS = {"password", "privatekey", "private_key", "token", "secret", "presharedkey"}


class InventoryError(ValueError):
    pass


def load_inventory(path=DEFAULT_INVENTORY):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _walk_keys(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = "%s.%s" % (prefix, key) if prefix else key
            yield path, key
            yield from _walk_keys(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_keys(child, "%s[%s]" % (prefix, index))


def validate_inventory(inventory):
    errors = []
    if inventory.get("schemaVersion") != 1:
        errors.append("schemaVersion must be 1")

    network = inventory.get("network", {})
    nodes = inventory.get("nodes", {})
    if not isinstance(nodes, dict) or len(nodes) < 2:
        errors.append("nodes must contain at least two entries")
        return errors

    try:
        asn = int(network["asn"])
        if not 1 <= asn <= 4294967295:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        errors.append("network.asn is invalid")

    try:
        v4_prefix = ipaddress.ip_network(network["ipv4Prefix"])
        if v4_prefix.version != 4:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        v4_prefix = None
        errors.append("network.ipv4Prefix is invalid")

    try:
        v6_prefix = ipaddress.ip_network(network["ipv6Prefix"])
        if v6_prefix.version != 6:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        v6_prefix = None
        errors.append("network.ipv6Prefix is invalid")

    router_ids = set()
    mesh_addresses = set()
    node_ids = set(nodes)
    for node_id, node in nodes.items():
        if not NODE_ID_RE.fullmatch(node_id):
            errors.append("invalid node id: %s" % node_id)
        try:
            router_id = ipaddress.ip_address(node["routerId"])
            if router_id.version != 4 or (v4_prefix and router_id not in v4_prefix):
                raise ValueError
            if router_id in router_ids:
                errors.append("duplicate routerId: %s" % router_id)
            router_ids.add(router_id)
        except (KeyError, TypeError, ValueError):
            errors.append("%s.routerId must be inside network.ipv4Prefix" % node_id)

        try:
            mesh_address = ipaddress.ip_address(node["meshIpv6"])
            if mesh_address.version != 6 or (v6_prefix and mesh_address not in v6_prefix):
                raise ValueError
            if mesh_address in mesh_addresses:
                errors.append("duplicate meshIpv6: %s" % mesh_address)
            mesh_addresses.add(mesh_address)
        except (KeyError, TypeError, ValueError):
            errors.append("%s.meshIpv6 must be inside network.ipv6Prefix" % node_id)

        stub = node.get("ospfStubInterface")
        if not isinstance(stub, str) or not IFACE_RE.fullmatch(stub):
            errors.append("%s.ospfStubInterface is invalid" % node_id)

        links = node.get("links", {})
        expected = node_ids - {node_id}
        if set(links) != expected:
            errors.append("%s.links must contain exactly: %s" % (node_id, ", ".join(sorted(expected))))
        interfaces = set()
        files = set()
        protocols = set()
        for peer_id, link in links.items():
            interface = link.get("interface") if isinstance(link, dict) else None
            if not isinstance(interface, str) or not IFACE_RE.fullmatch(interface):
                errors.append("%s.links.%s.interface is invalid" % (node_id, peer_id))
            elif interface in interfaces:
                errors.append("%s reuses interface %s" % (node_id, interface))
            interfaces.add(interface)
            try:
                cost = int(link["cost"])
                if not 1 <= cost <= 65535:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                errors.append("%s.links.%s.cost must be 1-65535" % (node_id, peer_id))
            file_name = link.get("file") if isinstance(link, dict) else None
            if not isinstance(file_name, str) or not NAME_RE.fullmatch(file_name):
                errors.append("%s.links.%s.file is invalid" % (node_id, peer_id))
            elif file_name in files:
                errors.append("%s reuses iBGP file %s" % (node_id, file_name))
            files.add(file_name)
            protocol = link.get("protocol") if isinstance(link, dict) else None
            if not isinstance(protocol, str) or not NAME_RE.fullmatch(protocol):
                errors.append("%s.links.%s.protocol is invalid" % (node_id, peer_id))
            elif protocol in protocols:
                errors.append("%s reuses iBGP protocol %s" % (node_id, protocol))
            protocols.add(protocol)
            if peer_id in nodes and node_id not in nodes[peer_id].get("links", {}):
                errors.append("link %s -> %s is not reciprocal" % (node_id, peer_id))

    for path, key in _walk_keys(inventory):
        normalized = key.lower().replace("-", "").replace("_", "")
        if normalized in {item.replace("_", "") for item in FORBIDDEN_KEYS}:
            errors.append("secret-like key is forbidden in inventory: %s" % path)
    return errors


def require_valid(inventory):
    errors = validate_inventory(inventory)
    if errors:
        raise InventoryError("\n".join("- %s" % error for error in errors))


def render_template(name, values=None):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in (values or {}).items():
        text = text.replace("{{%s}}" % key, str(value))
    leftovers = re.findall(r"\{\{[A-Z0-9_]+\}\}", text)
    if leftovers:
        raise InventoryError("unresolved template variables in %s: %s" % (name, ", ".join(leftovers)))
    return text


def render_ospf_area(node):
    lines = [
        "# Generated from inventory.json; do not edit on the node.",
        "area 0.0.0.0 {",
        '    interface "%s" { stub; };' % node["ospfStubInterface"],
    ]
    for peer_id, link in sorted(node["links"].items()):
        lines.extend([
            '    interface "%s" {' % link["interface"],
            "        cost %s;" % link["cost"],
            "        type ptp;",
            "    };",
        ])
    lines.extend(["};", ""])
    return "\n".join(lines)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_config(path, text):
    with Path(path).open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def render(inventory, output):
    require_valid(inventory)
    output = Path(output)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    nodes = inventory["nodes"]

    summaries = {}
    for node_id, node in sorted(nodes.items()):
        node_dir = output / node_id
        (node_dir / "ospf").mkdir(parents=True)
        (node_dir / "ibgp").mkdir(parents=True)
        write_config(node_dir / "ospf.conf", render_template("ospf.conf.tmpl"))
        write_config(node_dir / "ospf" / "0.conf", render_ospf_area(node))
        write_config(node_dir / "ibgp.conf", render_template("ibgp.conf.tmpl"))
        for peer_id in sorted(node["links"]):
            link = node["links"][peer_id]
            peer = nodes[peer_id]
            session = render_template("ibgp-session.conf.tmpl", {
                "PEER_ID": peer_id,
                "PEER_IPV6": peer["meshIpv6"],
                "PROTOCOL_NAME": link["protocol"],
            })
            write_config(node_dir / "ibgp" / (link["file"] + ".conf"), session)

        rendered_files = sorted(path for path in node_dir.rglob("*") if path.is_file())
        summaries[node_id] = {
            "routerId": node["routerId"],
            "meshIpv6": node["meshIpv6"],
            "files": {str(path.relative_to(node_dir)).replace("\\", "/"): _sha256(path) for path in rendered_files},
        }

    manifest = {
        "schemaVersion": 1,
        "source": "inventory.json",
        "nodes": summaries,
    }
    write_config(output / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--check", action="store_true", help="validate and render into a temporary directory")
    args = parser.parse_args()
    inventory = load_inventory(args.inventory)
    if args.check:
        with tempfile.TemporaryDirectory(prefix="dn42-network-check-") as temporary:
            manifest = render(inventory, temporary)
    else:
        manifest = render(inventory, args.output)
    print(json.dumps({"ok": True, "nodes": len(manifest["nodes"]), "files": sum(len(n["files"]) for n in manifest["nodes"].values())}))


if __name__ == "__main__":
    main()
