# DN42 network configuration

It is intentionally separate from
[`AndyYJF/dn42-peering`](https://github.com/AndyYJF/dn42-peering): this
repository owns the stable OSPF/iBGP core, while the Auto Peer service owns
dynamic external peer lifecycle.

## Ownership boundary

| Path on a node | Owner | Why |
|---|---|---|
| `/etc/bird/ospf.conf`, `/etc/bird/ospf/*` | this repository | OSPF topology and costs change through Git review |
| `/etc/bird/ibgp.conf`, `/etc/bird/ibgp/*` | this repository | the iBGP full mesh changes through Git review |
| `/etc/bird/peers/*` | Auto Peer agent / existing manual config | dynamic peers must not be deleted by a core rollout |
| `/etc/wireguard/dn42-<full-asn>.conf` | Auto Peer agent | peer lifecycle remains transactional and database-backed |
| node private keys, API tokens, passwords | node-local secret files | secrets are forbidden in `inventory.json` |

The initial scope deliberately does not render `bird.conf` or WireGuard. This
keeps the existing ROA/filter policy and all dynamic sessions outside the first
repository-managed rollout.

## Files

- `inventory.json`: public node identities, mesh addresses, interface names,
  and current OSPF costs.
- `templates/`: BIRD OSPF/iBGP templates.
- `render.py`: dependency-free validation and deterministic rendering.
- `audit.py`: read-only SSH audit of OSPF Full/PtP and iBGP Established state.
- `deploy.py` / `apply_core.py`: SSH-key staging plus locked, transactional node-side apply and rollback.
- `.rendered/`: generated output; ignored by Git.

## Local workflow

Validate without leaving generated files:

```bash
python3 render.py --check
python3 -m unittest discover -s . -p 'test_*.py' -v
```

Render files for inspection:

```bash
python3 render.py
find .rendered -type f -maxdepth 4 -print
```

Audit live routing through an SSH key or ssh-agent (passwords are intentionally
unsupported):

```bash
python3 audit.py --identity /path/to/deploy_key
python3 audit.py --node tyo --identity /path/to/deploy_key
```

Render locally, run a remote parse/health check, then explicitly apply one node:

```bash
python3 deploy.py --node hkt --mode local
python3 deploy.py --node hkt --mode remote-check --identity /path/to/deploy_key
python3 deploy.py --node hkt --mode apply --confirm-node hkt --identity /path/to/deploy_key
```

`apply_core.py` refuses unexpected paths, takes a non-blocking deployment lock,
requires a healthy pre-change core, parse-checks a temporary full BIRD tree,
backs up only the managed files, and restores them if reconfigure or the
300-second OSPF/iBGP recovery gate fails. It never writes `peers/`.

## Safe rollout sequence

Deployment is always explicit and node-scoped. The first rollout must be a
canary and preserve the dynamic peer directory:

1. Render and review the node diff.
2. Capture `audit.py` as the pre-change baseline.
3. Copy only `ospf.conf`, `ospf/`, `ibgp.conf`, and `ibgp/` to a remote staging
   directory. Never use `rsync --delete` against all of `/etc/bird`.
4. Overlay those files onto a copy of `/etc/bird` and run `bird -p -c bird.conf`.
5. Back up the live managed files, promote them, and run `birdc configure`.
6. Require three Full/PtP neighbors in both OSPF families and three Established
   iBGP sessions. Restore the backup if the check fails.
7. Roll out HKT first, then TYO, FRA, and LAX one at a time.

## Core-only nodes

`cn-shanghai` joined the OSPF/iBGP mesh later than the other nodes. It accepts
external eBGP peers through the Auto Peer service but every session requires
**manual approval** (`manualApproval: true` in `dn42-peering/config/nodes.json`)
because its international uplinks are capacity-limited.

Operational note: all cn-shanghai international uplinks have a physical path
MTU of 1280, and fragmented outer UDP is dropped. WireGuard interfaces keep
MTU 1420 (IPv6 needs >= 1280 on the link); instead TCP MSS is clamped to 1120
on `dn42-+` interfaces via node-local `dn42-mss-clamp.service`.

## Growing the mesh

Adding a node changes every node's expected neighbor count. The health gate in
`apply_core.py` is growth-safe: peers present in both the live and the staged
`ibgp/` tree must stay Full/Established (no regression), while newly added
peers only need to load into BIRD — they establish once the far end is
deployed. Roll out the existing nodes first, the new node last, then verify
all neighbors with `audit.py`.
