# Vendored: steward gate primitives

Source of truth: the console repo (vm-mgr-npm) `console/steward.py` and
`console/command_bus.py`, vendored here at **26e3a1a** so the eval harness runs
the SAME enforcement code the live gate does — authentic denials, authentic
audit events — without a runtime dependency on the console tree.

Pure, stdlib-only; the eval uses `decide`/`event`/`denial_detail`/`reach` and
`CommandBus.dispatch`. Re-vendor (and bump the hash) if the gate's decision or
event shape changes; `test_steward_pipeline.py` fails loudly if the metrics and
the gate's event schema drift apart.

## Local modifications (allowed, must be re-applied on re-vendor)

1. `command_bus.py` imports localized: `import steward` / `from steward import`
   → `from . import steward` / `from .steward import`. This makes the vendor
   dir a real subpackage, so the generic top-level names are never claimed
   process-wide (they could otherwise shadow — or be shadowed by — the
   console's own flat `steward`/`command_bus` when both trees co-reside in one
   interpreter, silently swapping enforcement code on first drift).
2. Consequence: `python3 command_bus.py` no longer runs standalone (relative
   imports); its self-tests run from the source-of-truth console repo instead.
   `python3 steward.py` still runs (no internal imports).
