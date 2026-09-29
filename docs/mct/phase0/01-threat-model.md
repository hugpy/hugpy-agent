# Threat model

Phase 0 deliverable (design §22). Scope: the Mediated Context Terminal (MCT)
prototype as specified in `mediated-context-terminal-design.md`. This document
names what we protect, who the adversaries are, and how each attack is stopped —
with pointers into the [enforcement-point registry](05-enforcement-point-registry.md).

## 1. Assets

| Asset | Why it matters |
|---|---|
| Operator intent (raw `operator_turn`) | Must reach A exactly; corruption changes the answer (invariant 2) |
| The host filesystem beyond permitted roots | A must never read it; B's ceiling bounds everything (§13.1) |
| Secrets on the host (`/etc/shadow`, SSH/cloud creds, keyrings) | Catastrophic if read or exfiltrated (§13.2) |
| Cross-session / cross-tenant objects | Isolation failure leaks other conversations (§7.4) |
| The durable ledger + object store | Authoritative recoverable state; tampering breaks audit (§17.1) |
| Provenance links | Loss makes summaries untraceable truth (invariant 8) |
| The operator's terminal | Escape-sequence / spoofed-provenance injection target (§16.3) |

## 2. Trust boundaries (design §4.1)

- **C → B:** C trusts B to preserve the exact message and render A faithfully.
- **B → everything else:** B treats A, source files, tool output, and retrieved
  text as **potentially adversarial input**. This is the core stance.
- **A → B:** A trusts only context objects whose manifest identifies origin and authority.
- **Sources:** trusted as *data from a named origin*, never as instructions.
- **Object store** = durable trusted state. **Inbox/outbox** = disposable transport.

## 3. Adversary model

| Adversary | Capability assumed | Goal |
|---|---|---|
| **Adversarial A** | Emits arbitrary pull requests, response bodies, and prose | Read outside scope, bypass B, render on wrong turn, spoof having-seen context |
| **Malicious source text** | Content inside a permitted file/log/page | Prompt-inject A or B into widening authority or changing policy |
| **Cross-session peer** | Holds a valid pointer from another session | Read another session's objects |
| **Compromised/confused local model** | B's ranking/summary model returns attacker-favorable output | Get a forbidden source ranked "relevant" and materialized |
| **Operator error** | Mistyped or contradictory instructions | Not an attacker, but must fail safe/legibly |
| **Local process attacker** | Can rename/symlink files between check and use | TOCTOU race to escape a source root |

Out of scope for the prototype (design non-goals §1.3): defeating a compromised
host kernel, and guaranteeing anything about an external provider's hidden KV cache.

## 4. Attack cases → mitigation (design §22.1)

Every case must **fail closed without damaging durable session state**.

| # | Attack | Mitigation | Invariant | Enforcement row |
|---|---|---|---|---|
| 1 | A asks B to ignore the whitelist | Whitelist is code/OS, not prose; pull text is untrusted | 12 | 11, 12 |
| 2 | A requests `/etc/shadow` via aliases / symlink chains | `openat2` under root fd, no symlink traversal — not `realpath` | 1, 5 | 9 |
| 3 | A embeds a host path in a pointer field | Pointer schema pattern rejects non-`mct://` values; resolver is a handle-table lookup | 5 | 1, 3 |
| 4 | Retrieved source text says "change policy" | Sources labeled data; authority metadata outside bytes; model can't authorize | 9 | 12, 17, 18 |
| 5 | Session X submits session Y's valid pointer | Object IDs + resolver are session-scoped; digest+scope check | (isolation) | 2, 3, 5 |
| 6 | A claims it already holds context it never opened | Adapter receipts, not A's prose, are the record of what was read | 6, 7 | 6, 7 |
| 7 | B's local model labels a forbidden source relevant | Ranking ≠ authorization; deterministic policy still runs | 9 | 11, 12, 17 |
| 8 | A returns a response for a cancelled turn | Turn/epoch/sequence check; cancelled turns never render | 14 | 7, 8 |
| 9 | A emits terminal escapes / pointer-shaped strings in prose | Output validation strips/blocks escapes; prose is never parsed as a pointer | 11 | 7 |
| 10 | Operator prompt conflicts with an older summary | Current turn is verbatim + required; conflicts retained & flagged, not silently merged | 2 | 7, and §11.5 |

## 5. Prompt-injection posture (design §13.6)

The single most important stance: **B's local model is advisory; deterministic
code and the OS are the security boundary.** Concretely —

- Retrieved files enter context as *data* with authority metadata attached
  outside the retrieved bytes.
- Source text cannot alter policy, allowed roots, or budgets.
- Instructions found in logs/pages/docs/comments are untrusted unless the
  operator explicitly promotes them.
- The local model may *recommend* a pull; only `capabilities.authorize()` grants it.
- A's own pull `need`/`reason` text is untrusted input to the broker.

## 6. Residual risks (tracked, not yet closed)

- **Provider KV cache:** treated as performance-only; never a correctness primitive (invariant, §12.1). No mitigation needed beyond *not trusting it*.
- **Covert channels via timing** across tenants: object store may dedupe bytes, but presence must not be observable via timing/IDs/errors (§7.4). Needs a Phase 6 test.
- **Operator approval fatigue:** excessive `approval_required` could train click-through. UX mitigation is a later concern; default policy should minimize prompts to genuinely sensitive ops.
- **Local model exfiltration via ranking:** a compromised local model can't authorize, but could bias which *permitted* context is surfaced. Bounded by the fact it only reorders already-permitted candidates.
