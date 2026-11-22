# Chore Time failover and restore contract

This contract is opt-in. Existing installations remain single-node unless
`house.json.failover.enabled` is exactly `true`.

## Authority and stable routes

A trusted coordinator issues one short lease at a time. The lease names one
node, carries a monotonically increasing epoch, lasts no more than the configured
maximum (120 seconds by default), and must not overlap a lease for another node.
Chore Time records the highest accepted epoch durably and rejects older epochs,
wrong-node leases, expired leases and overlong leases. A primary losing its lease
exits its household workers; a restarted node without authority serves read-only
status/pages and rejects mutations.

The coordinator, not Chore Time, owns the stable LAN route. Existing Hub URLs
remain `/kids`; parent UI remains `/inspect`; state remains `/api/state`; health
is `/health`. Home Assistant continues to serve `/local/chore-kids.html` and
consume `sensor.chore_state`. A promotion is incomplete until those routes and
the HA asset resolve to the promoted revision.

A reverse proxy normally hides the physical Hub's source IP. Configure the
dedicated LAN ingress to remove any client-supplied identity/token headers, add
a private random token, and append the observed client to `X-Forwarded-For`.
On both prospective writers configure the same protected token file and the
actual ingress-pod source range:

```json
{
  "trusted_display_proxy": {
    "token_file": "/protected/chore-proxy.token",
    "peer_cidrs": ["TRAEFIK_POD_CIDR"],
    "token_header": "X-Chore-Proxy-Token",
    "forwarded_header": "X-Forwarded-For"
  }
}
```

The token file must be mode `0600`. The app accepts only the rightmost forwarded
address, only when the TCP peer is inside a configured proxy CIDR, the token is
correct, and that address already belongs to a configured Hub/TV. This identity
is used only for display-bound child sessions, heartbeats and UI behavior. Raw
TCP peer identity still controls loopback and parent authorization, so a proxy
request can never become a localhost request. Before moving a Hub, prove two
distinct real devices remain distinct and that a forged leftmost forwarded
address cannot claim either device.

## State capture boundary

The files do not form a multi-file transaction. A copy made while the process is
running is therefore not a consistent backup, even if `ledger.jsonl` happens to
parse. Capture is valid only after the coordinator has stopped renewing the old
lease, the old Chore process has exited (or has been explicitly stopped), and no
new primary has started. Copy the data directory from that quiescent boundary,
then run:

```sh
python3 tools/failover_replay.py manifest --data SNAPSHOT --revision DEPLOYED_GIT_SHA --output manifest.json
```

`manifest.json` contains no household payload: only relative paths, sizes,
SHA-256 hashes, validation booleans and record counts. It fails on a missing
core file, malformed/truncated ledger tail, duplicate event id, duplicate
idempotency key, invalid config JSON, or a newly introduced unclassified file.
Never auto-truncate an incomplete ledger record. Reconcile it against the last
acknowledged transaction or restore the last valid capture.

## Authoritative versus rebuildable

The three required files are `ledger.jsonl` (points/jobs/rewards and other core
events), `chores.json` (private family policy and PIN hashes), and `house.json`
(house integration/configuration). The manifest classifies as authoritative all
in-flight approval/security state, side-effect dedupe journals, vacuum queues,
learned robot/room-eye state, packing/news definitions, local operational
preferences, tokens, calibration artifacts and their referenced images. These
must travel together in the encrypted capture.

Interaction logs, generated calendar caches, per-display news/open-page state,
and lock files are rebuildable. Old `*-pre-*` files and `backups/` are historical
archives, not the active restore boundary. Lease, watermark and failover probe
token files are node-local fencing material: provision them independently and
never restore an old node's authority lease onto a new primary.

The exact lists live in `tools/failover_replay.py`. An unclassified file blocks
proof so the contract must be reviewed whenever a new persistence file appears.

## Restore and replay

Restore the encrypted authoritative files with their modes while both nodes are
stopped. Verify the restored copy with `manifest`, using the exact deployed code
revision, then compare it with the capture:

```sh
python3 tools/failover_replay.py compare capture-manifest.json restore-manifest.json
```

Pass requires the same app revision, matching hashes for every required or
optional-authoritative file, equal valid ledger record counts, and no missing or
unclassified files. After a pass, provision a fresh node-local lease with an
epoch greater than every previous epoch. Do not start the old node until it has
the winning data and a still-higher lease; an old epoch remains fenced.

The synthetic rehearsal is:

```sh
python3 tools/failover_replay.py drill
```

It creates temporary data containing only `failover_test`, runs with all
household modules/effects suppressed, commits one real `adjust` event, retries
the same transaction, returns under a higher epoch, and proves one ledger row
and one balance change. Reusing the transaction id with different points is
rejected. A later attempt with the older epoch is fenced.

This exactly-once guarantee currently covers the explicit failover probe only.
Most ordinary Chore HTTP mutations do not yet accept durable client idempotency
keys. The single non-overlapping lease prevents two primaries, but an ordinary
client retry after a lost response can still duplicate an endpoint that lacks
its own existing request-id guard. Do not claim transparent production failover
for arbitrary in-flight child/parent actions until those endpoints are covered
or the promotion procedure drains them before capture.

## Backup report and cleanup

Backup automation may report only: contract version, app revision, pass/fail,
event count, relative paths, sizes and hashes. It must not report JSON rows,
tokens, names, balances or other payloads. Keep the encrypted capture and its
manifest until a restored isolated copy passes. Cleanup means stopping the
isolated process, deleting its temporary decrypted directory and probe token,
and retaining only the encrypted capture plus the non-sensitive manifest and
receipt. Never use the household data directory as a drill target.
