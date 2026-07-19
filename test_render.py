import copy
import json
import tempfile
import unittest
from pathlib import Path

import render
import audit


class NetworkRenderTests(unittest.TestCase):
    def setUp(self):
        self.inventory = render.load_inventory()

    def test_production_inventory_is_a_valid_four_node_full_mesh(self):
        self.assertEqual(render.validate_inventory(self.inventory), [])
        self.assertEqual(set(self.inventory["nodes"]), {"tyo", "hkt", "fra", "lax"})
        for node in self.inventory["nodes"].values():
            self.assertEqual(len(node["links"]), 3)

    def test_render_is_deterministic_and_separates_dynamic_peers(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            one = render.render(self.inventory, first)
            two = render.render(self.inventory, second)
            self.assertEqual(one, two)
            for node_id in self.inventory["nodes"]:
                node_dir = Path(first) / node_id
                self.assertTrue((node_dir / "ospf.conf").exists())
                self.assertEqual(len(list((node_dir / "ibgp").glob("*.conf"))), 3)
                self.assertFalse((node_dir / "peers").exists())
                for config in node_dir.rglob("*.conf"):
                    self.assertNotIn(b"\r\n", config.read_bytes())

    def test_rendered_sessions_use_repository_mesh_addresses(self):
        with tempfile.TemporaryDirectory() as output:
            render.render(self.inventory, output)
            session = (Path(output) / "tyo" / "ibgp" / "hkt.conf").read_text()
            self.assertIn("dn42_ibgp_hkt", session)
            self.assertIn("fdd2:e3e2:c922::4", session)

    def test_missing_full_mesh_link_is_rejected(self):
        broken = copy.deepcopy(self.inventory)
        del broken["nodes"]["tyo"]["links"]["fra"]
        self.assertTrue(any("tyo.links must contain exactly" in error for error in render.validate_inventory(broken)))

    def test_secret_like_inventory_keys_are_rejected(self):
        broken = copy.deepcopy(self.inventory)
        broken["nodes"]["tyo"]["privateKey"] = "must-not-be-committed"
        self.assertTrue(any("secret-like key" in error for error in render.validate_inventory(broken)))

    def test_live_audit_parser_requires_three_full_neighbors_and_ibgp_sessions(self):
        ospf = """dn42_ospf:
a Full/PtP
b Full/PtP
c Full/PtP
dn42_ospf6:
a Full/PtP
b Full/PtP
c Full/PtP
"""
        protocols = """dn42_ibgp_ge-nc BGP --- up now Established
dn42_ibgp_hkt BGP --- up now Established
dn42_ibgp_lax BGP --- up now Established
"""
        result = audit.evaluate_node("tyo", self.inventory["nodes"]["tyo"], ospf, protocols)
        self.assertTrue(result["ok"])

    def test_live_audit_parser_reports_a_down_ibgp_session(self):
        ospf = """dn42_ospf:\na Full/PtP\nb Full/PtP\nc Full/PtP
dn42_ospf6:\na Full/PtP\nb Full/PtP\nc Full/PtP
"""
        protocols = """dn42_ibgp_ge-nc BGP --- up now Established
dn42_ibgp_hkt BGP --- start now Active
dn42_ibgp_lax BGP --- up now Established
"""
        result = audit.evaluate_node("tyo", self.inventory["nodes"]["tyo"], ospf, protocols)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ibgp"]["down"], ["dn42_ibgp_hkt"])


if __name__ == "__main__":
    unittest.main()
