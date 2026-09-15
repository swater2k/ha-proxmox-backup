# Proxmox Backup Server for Home Assistant

A custom integration that brings a Proxmox Backup Server (PBS) into Home Assistant
over its REST API: host health, datastore usage, garbage collection, verification
coverage, and every backup group as its own device.

## What this integration cannot do

**PBS cannot start a backup.** Backups are always pushed by the client, that is by
Proxmox VE (`vzdump`) or by `proxmox-backup-client`. There is no "back up CT 107
now" endpoint in the PBS API. A button for that belongs on the PVE side, not here.

Only PBS-side operations are controllable: garbage collection, verify, prune,
forget, protect, maintenance mode, and running configured jobs.

## Installation

> **Watch out for domain conflicts:** this integration uses the domain `pbs` and
> the folder `custom_components/pbs/`. The integration
> `thecodingdad/ha-proxmox-backup` uses `proxmox_backup` — the two can be
> installed side by side, but never two integrations sharing one domain.

1. In HACS, add this repository under *Custom repositories* with the category
   *Integration*.
2. Download "Proxmox Backup Server" and restart Home Assistant.
3. Go to *Settings → Devices & services → Add integration → Proxmox Backup Server*.

To remove it, delete the integration under *Devices & services*. No files are left
behind and nothing on the PBS is changed. Revoke the API token afterwards.

## Creating an API token

PBS intersects user and token privileges. The ACL therefore has to be granted
**twice**, once to the user and once to the token:

```bash
proxmox-backup-manager user create ha@pbs
proxmox-backup-manager user generate-token ha@pbs homeassistant
# The "value" field is the secret and is shown exactly once.

proxmox-backup-manager acl update /datastore/<datastore> DatastoreAdmin --auth-id 'ha@pbs!homeassistant'
proxmox-backup-manager acl update /datastore/<datastore> DatastoreAdmin --auth-id 'ha@pbs'
proxmox-backup-manager acl update /system Audit --auth-id 'ha@pbs!homeassistant'
proxmox-backup-manager acl update /system Audit --auth-id 'ha@pbs'
```

`Audit` on `/system` provides node status, services, certificates, tasks, updates
and the disk list including SMART. Without it everything else still works: the
affected entities simply stay unavailable, and the log names the denied endpoint
once.

If you never want to trigger actions, grant `DatastoreAudit` instead of
`DatastoreAdmin` — all sensors still work, the actions just have no effect.

## Configuration

| Field | Meaning |
|---|---|
| Host / Port | Address of the PBS, default port 8007 |
| Token ID | The full id, e.g. `ha@pbs!homeassistant` |
| Token secret | The `value` from `generate-token` |
| Verify TLS certificate | Turn off for the self-signed certificate PBS ships with |

You then pick which of the visible datastores to monitor; only those get entities.
The options can be changed later: datastore selection, the staleness threshold,
the usage warning and critical thresholds, the garbage collection warning, and the
two switches for write and destructive actions (both off by default).

## Data updates

Three separate polling intervals, because the data changes at very different rates:

| Interval | Contents |
|---|---|
| 60 s | Host load, datastore usage, active reads and writes, running tasks |
| 5 min | Backup groups, snapshots, garbage collection, jobs, failed tasks |
| 6 h | Version, services, certificates, updates, disks and SMART |

Above roughly 1500 snapshots the per-snapshot fetch is throttled to about hourly;
counts and groups then come from the cheaper count that PBS keeps itself.

## Entities

**PBS instance device:** CPU, IO wait, load (1/5/15), memory, swap, root
filesystem, boot time, running tasks, failed tasks (24 h / 7 d), last failed task,
version, available updates, certificate expiry, subscription, connectivity,
service problem, disk problem.

**One device per backup group** (per LXC, VM or host): last backup, backup age in
days, snapshot count, size, verification state, owner, and a problem sensor for a
stale backup. Devices are named after the guest name that PVE writes into the
snapshot comment through its `notes-template`, so `vaultwarden (CT 107)`. Without
that comment the name falls back to `Host pbs`. New groups appear on the next
poll, no restart needed.

**One device per datastore:** used / free / total, usage percentage, estimated
full date, active reads and writes, backup groups, snapshots, verified / failed /
unverified snapshots, oldest and newest snapshot, deduplication factor, garbage
collection time / state / duration / freed / pending / schedule, GC running,
maintenance mode, and an aggregated problem sensor listing its reasons as an
attribute.

**Overall status:** one enum sensor folds usage, garbage collection, verification,
backup freshness, jobs, services and disks into `ok`, `warning` or `critical`,
with the individual findings in the `findings` attribute. Alongside it a problem
binary sensor for automations, plus counters for stale groups and the oldest
backup.

Each configured prune, verify and sync job adds two sensors to its datastore
device: the result of the last run and when it finished, with the schedule and the
next run as attributes.

Rarely needed values (load 5/15, total memory, IO wait, failed tasks 7 d, GC
pending) are disabled by default and can be enabled per entity.

Entity ids follow the language of your Home Assistant installation, so a German
install yields `sensor.…_gesamtstatus` where an English one yields
`sensor.…_overall_status`.

## Actions

By default the integration creates **no** controls at all. Two separate switches
in the options unlock them:

| Switch | Unlocks |
|---|---|
| Allow write actions | Garbage collection, verify, job runs, maintenance mode |
| Allow destructive actions | Prune and forget (requires write actions) |

With write actions enabled you get buttons per datastore (start garbage
collection, verify datastore), per backup group (verify), per job (run now), and a
select for the maintenance mode offering `none`, `read-only` and `offline`.

Destructive operations deliberately get **no button**. A mis-tap in a dashboard
would be one heartbeat away from irreversible loss, and Home Assistant does not
ask for confirmation on a button press. They are available only as actions with
mandatory parameters:

| Action | Device | Note |
|---|---|---|
| `pbs.run_garbage_collection` | Datastore | — |
| `pbs.verify` | Datastore or group | can skip already verified snapshots |
| `pbs.prune` | Group | `dry_run` defaults to **on**, returns the result |
| `pbs.forget_group` | Group | requires `confirm: true` |
| `pbs.forget_snapshot` | Group | requires `confirm: true` and a timestamp |
| `pbs.set_protected` | Group | protects against pruning |

Extra safeguards: before any deletion the datastore is checked for running write
operations — if a backup is in progress, the action aborts. Groups holding
protected snapshots are refused. And a prune without any `keep_*` value is
rejected, because it would remove everything.

### Dry run first

```yaml
action: pbs.prune
data:
  device_id: <device id of the backup group>
  dry_run: true
  keep_last: 3
  keep_daily: 7
response_variable: result
```

`result` lists, per group, the snapshots a real run would remove, along with the
counts. Once that looks right, repeat it with `dry_run: false`.

### Events

Long running operations are not awaited. The integration remembers the UPID,
polls it every 15 seconds and reports the outcome as a `pbs_task_finished` event
carrying `action`, `target`, `state` and `status`. While something is running the
action-running binary sensor is on and the last-action sensor reads `running`,
`ok` or `error`.

Every irreversible operation additionally fires `pbs_destructive_action` with the
datastore, the target, the affected snapshots and the id of the user who
triggered it — meant as an anchor for an audit automation.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "No datastore visible" during setup | The ACL is missing on the user behind the token. Run both lines from the section above. |
| Disk problem sensor unavailable | `Audit` on `/system` is missing. `/system/status` alone does not cover `/system/disks`. |
| Host unreachable | TLS verification is on while PBS uses its self-signed certificate. |
| Token rejected | The reauth dialog appears automatically; enter a new secret there. |

For bug reports, the integration's diagnostics download helps: it contains the raw
API responses with secrets and identifiers redacted.

## Icon

The icon lives at `custom_components/pbs/brand/icon.svg` with rendered `icon.png`
(256 px) and `icon@2x.png` (512 px). It shows stacked restore points with a check
mark for passed verification. Deliberately not the Proxmox logo, which is a
protected trademark.

Listing in the official HACS catalogue would additionally require the same artwork
in `home-assistant/brands` under `custom_integrations/pbs/`. For installation via
a custom repository the local folder is enough.

## A note on sizes

The size of a backup group is the **logical** sum of all its snapshots, not the
space occupied on disk. Nine nearly identical snapshots share their chunks, so the
sum over all groups is a multiple of what the datastore actually uses. The
deduplication factor on the datastore device shows the ratio.

## Development

Capture test data from your own instance:

```bash
python3 scripts/dump_fixtures.py \
  --host 192.168.178.138 --token-id 'ha@pbs!homeassistant' \
  --token-secret '…' --insecure
```

This writes every API response to `tests/fixtures/`; denied endpoints land in
`_errors.json`. Those files are the basis for the unit tests.

## Status

- **M1 – done:** API client, config flow with datastore selection, reauth and
  reconfigure, three coordinators, instance and datastore entities, diagnostics.
- **M2 – done:** a device per backup group with resolved guest names, dynamic
  discovery of new groups, removal of orphaned devices, job entities, overall
  status.
- **M3 – done:** actions (GC, verify, prune, forget, protect, jobs, maintenance
  mode), task tracking via UPIDs, events, safety switches.
- **M4:** repair issues for missing permissions, icon translations, custom CA path.
- **M5:** tests against the fixtures, CI, example dashboard.
