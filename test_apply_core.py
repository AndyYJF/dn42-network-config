import subprocess
import tempfile
import unittest
import os
from pathlib import Path

import apply_core


OSPF_HEALTHY = """dn42_ospf:
a Full/PtP
b Full/PtP
c Full/PtP
dn42_ospf6:
a Full/PtP
b Full/PtP
c Full/PtP
"""
PROTOCOLS_HEALTHY = """dn42_ibgp_a BGP --- up now Established
dn42_ibgp_b BGP --- up now Established
dn42_ibgp_c BGP --- up now Established
"""


def write_tree(root, label="new"):
    root = Path(root)
    (root / "ospf").mkdir(parents=True)
    (root / "ibgp").mkdir(parents=True)
    (root / "ospf.conf").write_text("# %s ospf root\n" % label)
    (root / "ibgp.conf").write_text("# %s ibgp root\n" % label)
    (root / "ospf" / "0.conf").write_text("# %s ospf area\n" % label)
    for suffix in ("a", "b", "c"):
        (root / "ibgp" / (suffix + ".conf")).write_text(
            "protocol bgp 'dn42_ibgp_%s' from ibgpeers { neighbor fd00::1 as OWNAS; };\n" % suffix
        )


class ApplyCoreTests(unittest.TestCase):
    def test_staging_accepts_only_three_core_ibgp_sessions(self):
        with tempfile.TemporaryDirectory() as staging:
            write_tree(staging)
            self.assertEqual(
                apply_core.validate_staging(staging),
                ["dn42_ibgp_a", "dn42_ibgp_b", "dn42_ibgp_c"],
            )

    def test_staging_rejects_dynamic_peer_paths(self):
        with tempfile.TemporaryDirectory() as staging:
            write_tree(staging)
            (Path(staging) / "peers").mkdir()
            (Path(staging) / "peers" / "dn42_4242420001.conf").write_text("do not touch\n")
            with self.assertRaisesRegex(apply_core.ApplyError, "unmanaged path"):
                apply_core.validate_staging(staging)

    def test_replace_and_restore_leave_dynamic_peers_untouched(self):
        with tempfile.TemporaryDirectory() as staging, tempfile.TemporaryDirectory() as bird, tempfile.TemporaryDirectory() as backup:
            write_tree(staging, "new")
            write_tree(bird, "old")
            peers = Path(bird) / "peers"
            peers.mkdir()
            dynamic = peers / "dn42_4242420001.conf"
            dynamic.write_text("dynamic peer\n")
            backup_dir = Path(backup) / "snapshot"
            apply_core.backup_managed(bird, backup_dir)
            apply_core.replace_managed(staging, bird)
            self.assertIn("new", (Path(bird) / "ospf.conf").read_text())
            if os.name != "nt":
                self.assertEqual((Path(bird) / "ospf").stat().st_mode & 0o777, 0o755)
            self.assertEqual(dynamic.read_text(), "dynamic peer\n")
            apply_core.restore_managed(bird, backup_dir)
            self.assertIn("old", (Path(bird) / "ospf.conf").read_text())
            self.assertEqual(dynamic.read_text(), "dynamic peer\n")

    def test_health_snapshot_requires_both_ospf_families_and_all_ibgp(self):
        def runner(command, cwd=None, timeout=60):
            output = OSPF_HEALTHY if "ospf" in command else PROTOCOLS_HEALTHY
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        health = apply_core.health_snapshot(
            ["dn42_ibgp_a", "dn42_ibgp_b", "dn42_ibgp_c"], 3, runner,
        )
        self.assertTrue(health["ok"])

    def test_failed_reconfigure_restores_previous_core(self):
        with tempfile.TemporaryDirectory() as staging, tempfile.TemporaryDirectory() as bird, tempfile.TemporaryDirectory() as backups:
            write_tree(staging, "new")
            write_tree(bird, "old")
            (Path(bird) / "bird.conf").write_text("include core\n")
            (Path(bird) / "peers").mkdir()
            dynamic = Path(bird) / "peers" / "dynamic.conf"
            dynamic.write_text("dynamic peer\n")
            configure_calls = 0

            def runner(command, cwd=None, timeout=60):
                nonlocal configure_calls
                if command[:2] == ["bird", "-p"]:
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="warnings only")
                if command == ["birdc", "configure"]:
                    configure_calls += 1
                    output = "Reconfiguration failed" if configure_calls == 1 else "Reconfigured"
                    return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
                if "ospf" in command:
                    return subprocess.CompletedProcess(command, 0, stdout=OSPF_HEALTHY, stderr="")
                return subprocess.CompletedProcess(command, 0, stdout=PROTOCOLS_HEALTHY, stderr="")

            with self.assertRaisesRegex(apply_core.ApplyError, "previous managed configuration restored"):
                apply_core.apply(staging, bird, backups, runner=runner)
            self.assertIn("old", (Path(bird) / "ospf.conf").read_text())
            self.assertEqual(dynamic.read_text(), "dynamic peer\n")
            self.assertEqual(configure_calls, 2)


if __name__ == "__main__":
    unittest.main()
