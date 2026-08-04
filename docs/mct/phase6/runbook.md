# MCT operational runbook

Deployment and operations for the Mediated Context Terminal. The application-level
controls are code + tests (Phases 0–6); this runbook covers the host/fleet
configuration that lives outside the process. Ties together design §13.2, §19,
§17.2, §23.3 and the Phase 0 [service-account plan](../phase0/02-service-account-and-mounts.md).

## 1. Provision the B service account (mandatory)

B must run as a dedicated, unprivileged account — **never** the installing user.
The startup preflight (`hardening.assert_hardened(enforce=True)`) refuses to start
otherwise (registry row 15).

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin mct-broker
# verify: not in sudo/wheel/docker/lxd/kvm/adm, uid != 0
id mct-broker
```

Add the preflight to the service entrypoint:

```python
from hugpy_agent.mct import hardening
hardening.assert_hardened(enforce=True)   # raises HardeningError -> unit fails
```

## 2. systemd hardening unit

Finalize the Phase 0 draft; key directives:

```ini
[Service]
User=mct-broker
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
# IPAddressAllow only the specific A/provider endpoints, if any
ReadOnlyPaths=/srv/mct/sources
ReadWritePaths=/var/lib/mct
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
MemoryMax=…  TasksMax=…  CPUQuota=…
```

Mounts: read-only bind for permitted source roots; a separate writable
object-store/spool mount; per-session disk quota via `BrokerConfig.session_quota_bytes`.

## 3. Network / transport posture (§19.3)

- **Local A on the same host:** prefer the Unix-domain control socket. Claude Code
  as A runs as a confined subprocess (`--strict-mcp-config`), no network needed.
- **Remote A / fleet:** mutually-authenticated TLS with short-lived channel
  identity; per-station policy snapshots; adapter version negotiation. The object
  resolver is **never** a general HTTP file server — no unauthenticated object URLs
  or reusable signed links in logs.
- **Egress default-deny.** B owns all outbound source access; A gets no network.

Fleet metadata can move from SQLite WAL to PostgreSQL (§14.2) keeping the same
event/object schema; the ledger's thread-local-connection model already supports
one broker serving many concurrent sessions on a single host.

## 4. Quotas, admission, GC

- Set `session_quota_bytes` per tenant; over-quota commits fail closed (existing
  state preserved).
- Schedule `gc.garbage_collect(server, dry_run=False)` off-peak. It only reclaims
  orphaned bytes (unreferenced by any ledger object); pass `legal_hold` for
  retained digests. Objects are quarantined, not hard-deleted, within the window.
- Admission control: cap concurrent turns; back-pressure new input behind the
  active turn (§15.1).

## 5. Monitoring

Emit `telemetry.turn_report` / `session_summary` to your metrics pipeline. These
are **content-safe** (object IDs, digests, decisions, timing — never bodies,
§18.3). Alert on: rising omission/denial rates, epoch-change storms (A instability),
render/validation retries, quota pressure, ledger unavailability (stop accepting
turns, §17).

## 6. Staged rollout with rollback (§23.3)

1. **Shadow.** Run the baseline path authoritative; B builds the curated manifest
   and records projected token savings vs full context (`tools/phase5_eval.py`
   style). No mediated answers shown.
2. **Canary.** Make the mediated path authoritative for one station/tenant. Watch
   the monitoring signals above and the quality suite.
3. **Expand** tenant by tenant. Keep the baseline path warm.
4. **Rollback.** Because the object store + append-only ledger are authoritative
   and all transitions are idempotent, flip the route back to baseline at any
   time with no state loss; in-flight turns resume or fail visibly (§17).

## 7. Recovery drills

- Kill B mid-turn at each state; on restart `recovery.reconcile` reports
  unfinished turns and verifies chains; resent `response.ready` renders at most
  once. Covered by `tests/recovery/test_fault_injection.py`.
- Corrupt an object file → resolver quarantines it and denies the read
  (integrity error), turn fails visibly.
- Ledger unavailable → B stops accepting turns rather than running without audit.
