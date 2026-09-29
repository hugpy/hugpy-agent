# Service-account and mount plan

Phase 0 deliverable (design §22, grounded in §13.2–§13.4 and §19.3). Defines the
OS-level ceiling for B — `Reach_B,OS`, the outermost term of the capability
intersection (§13.1). Everything MCT enforces in code sits *inside* this ceiling;
this document keeps the ceiling itself low.

## 1. Process identity (design §13.2)

B **must** run as a dedicated, unprivileged service account — not the installing
user, and not any account with root-equivalent group membership.

Hard requirements:

- Dedicated UID/GID, e.g. `mct-broker`.
- **Not** a member of `sudo`, `wheel`, `docker`, `lxd`, `kvm`, or any
  root-equivalent group.
- No inherited SSH agent socket, cloud credential env, desktop keyring, or broad
  environment secrets.
- No login shell; `serve`/daemon entry only.

### Preflight gate (design §20.4, §20.9)

Deployment **must stop** if the account running B has root-equivalent groups.
This is a deterministic startup check, not advice:

```text
if effective account ∈ {sudo, docker, lxd, wheel, kvm, root-gid}:
    refuse to start MCT; emit remediation message
```

The published `hugpy-agent` currently ships a systemd **user** service running
under the installing user (design §20.4). That is unacceptable for MCT B — a
dedicated account or a separate hardened MCT worker is required before any
non-prototype deployment.

## 2. Mount / filesystem layout (design §13.2, §7.4, §20.7)

| Mount | Mode | Purpose |
|---|---|---|
| Permitted source roots | **read-only bind** | L4 sources B may snapshot for A |
| Object store + spool | read-write, private | `objects/`, `runtime/inbox/`, `runtime/outbox/`, ledger |
| Temp dir | private (`PrivateTmp`) | scratch; never a trust path |
| Everything else | not mounted / inaccessible | default-deny |

Prototype store lives beside the existing journal without touching the
crash-critical schema (design §20.7):

```text
<workspace>/.hugpy_agent/
├── journal.db            # existing hugpy-agent runs/messages/tool calls (untouched)
├── memory_vectors.db     # existing optional RAG index
├── mct/
│   ├── mct.db            # objects, events, epochs, capabilities, receipts
│   ├── objects/sha256/…  # content-addressed immutable store
│   ├── runtime/inbox/…   # read-only-to-A materialized objects
│   └── runtime/outbox/…  # write-only-to-A response/pull spool
└── traces/…
```

`mct.db` references the existing `run_id` as an application-level foreign
identity; no migration of `journal.db` during experimentation.

## 3. Safe path resolution (design §13.3)

Authorization is **descriptor-based, not string-prefix**. `realpath()` alone is
insufficient — it leaves a TOCTOU race between check and open.

On Linux, prefer `openat2(2)` rooted at a pre-opened allowed-directory fd with
`RESOLVE_*` constraints:

- `RESOLVE_BENEATH` — resolve only beneath the allowed root.
- `RESOLVE_NO_SYMLINKS` — reject symlink traversal where policy requires.
- `RESOLVE_NO_MAGICLINKS` — reject `/proc/*/fd`-style magic links.
- `RESOLVE_NO_XDEV` — reject mount-point escapes if configured.
- Fallback path uses `O_NOFOLLOW` + descriptor-relative `openat` traversal.

This is enforcement row 9 in the [registry](05-enforcement-point-registry.md).
Snapshots (row 10) copy the bytes read through this confined descriptor into the
immutable store, so A never touches a live file (design §13.4).

## 4. systemd hardening (illustrative)

A starting unit for the hardened worker (to be finalized in Phase 6):

```ini
[Service]
User=mct-broker
Group=mct-broker
SupplementaryGroups=
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
# IPAddressAllow only the specific provider endpoints A needs, if any
ReadOnlyPaths=/permitted/source/root
ReadWritePaths=/var/lib/mct
# cgroup limits
CPUQuota=…  MemoryMax=…  TasksMax=…  IOWeight=…
```

Plus per-session disk quotas on the object store (design §13.2, §17 "disk quota
reached").

## 5. Network posture (design §19.3)

- Prefer a **local Unix-domain socket** for the B↔A control plane when co-located.
- Remote A adapters: mutually authenticated TLS, short-lived channel identity.
- The object resolver is **not** a general HTTP file server; no unauthenticated
  object URLs, no reusable signed links in logs.
- **B owns all outbound source access.** A gets no general network client unless
  separately granted — egress default-deny (`IPAddressDeny=any` above).

## 6. Privileged operations enumerated (exit-condition cross-check)

Every OS-privileged action B can take, and where it is bounded:

| OS-privileged action | Bounded by |
|---|---|
| Reading a source file | ro bind mount + `openat2` under root fd (§3) |
| Writing an object | RW spool only; atomic commit (registry row 4) |
| Opening a network connection | egress default-deny; explicit allow-list (§5) |
| Spawning a process (executor) | separate approved-action flow only (registry row 14) |
| Consuming disk/CPU/mem | cgroups + per-session quota (§4) |

No row above depends on model judgement. This satisfies the Phase 0 exit
condition together with [`05-enforcement-point-registry.md`](05-enforcement-point-registry.md).
