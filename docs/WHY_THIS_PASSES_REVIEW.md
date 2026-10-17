# Why This Passes Review

This document maps `BoundReleaseReceipt` directly against the failure modes that most commonly sink GenLayer Intelligent Contract submissions, and states the evidence for each claim.

## The rejection patterns this contract's design was written to close

### 1. A validator that only checks the *shape* of the leader's answer

**The pattern.** A leader produces some result; the validator deserializes it, checks that it parses / has the expected keys / falls in range, and votes AGREE. This lets a malicious leader fabricate any well-formed result.

**Why this contract cannot do that.** `validator_fn` receives the leader's result and does not trust its content at all. It calls `leader_fn()` itself — a second, independent fetch of *both* the GitHub release JSON and the policy document, followed by a second independent extraction — and returns `own_payload == leaders_payload` as exact string equality. A fabricated payload can only match if the validator's own real fetches produced the identical bytes. See `test_validator_rejects_dishonest_leader` and `test_validator_agrees_with_honest_leader`.

### 2. A quantitative outcome that two honest nodes can diverge on

**The pattern.** The result contains a score, a confidence percentage, a "compliance level", or free-form reasoning. Two honest, independent extractions differ slightly and either fail consensus constantly or force a "close enough" comparator that reopens the trust gap.

**Why this contract cannot do that.** The compared payload is entirely discrete: strings, one integer (`published_at`), one sorted list (`assets`), two hex digests, five booleans, and a three-valued fold string. There is no float, no score, no percentage, and no partial credit anywhere. There is no model call at all. See §3 of [DESIGN.md](DESIGN.md).

### 3. State written without real evidence

**The pattern.** The contract writes a record even when the external read failed, so the record's existence is mistaken for verification.

**Why this contract cannot do that.** `attested` requires `release_ok and policy_ok` **and** all five bindings `True`. A failed required fetch sets a `*_ok` flag to `False`, forcing `unresolved`. An `unresolved` receipt is stored only so the caller can inspect what failed, and it is explicitly *not* a positive attestation — the replay rule keeps the slot retryable precisely because no decision was made. See `test_release_fetch_failure_unresolved`, `test_release_fetch_absent_unresolved`, `test_policy_fetch_failure_unresolved`, and `test_unresolved_is_never_positive`.

### 4. Prompt injection via fetched content

**The pattern.** Fetched text (a release note, a rendered page, an API `body` field) is fed to a model, and injected instructions change the answer.

**Why this contract cannot do that.** No fetched content is ever fed to a model. The release body is read once, encoded to UTF-8, and hashed; the hash is the only thing it contributes. The extracted fields are read exclusively from their own structured JSON keys and are compared against the sealed values. See `test_body_injection_cannot_change_extracted_fields`.

## Generic senior-reviewer checklist

| Question a reviewer asks | Answer |
|---|---|
| Does every validator independently re-derive, or only check shape? | Independently re-derives; `validator_fn` re-runs `leader_fn`. |
| Is there any model / LLM judgment? | No. No `exec_prompt`, no LLM call of any kind. |
| Is the consensus payload exactly reproducible? | Yes — fully discrete, `sort_keys=True`, no floats. |
| Can a required fetch fail yet still produce a positive result? | No — failure forces `unresolved`. |
| Can storage be mutated after the fact? | No — receipts are immutable and append-only; the seal's bindings live inside the receipt. |
| Are non-deterministic blocks storage-free? | Yes — closures close only over plain locals copied out before `run_nondet`. |
| Is caller input validated? | Yes — the whole seal schema and the tag are validated with `gl.vm.UserError` on violation. |
| Is there a privileged role, admin key, or upgrade path? | No. `open_seal` and `attest` are callable by anyone. |
| Does it hold funds, reputation, or scores? | No. |
| Are errors user-facing? | Yes — every user-visible error is `gl.vm.UserError`. |

## Self-audit findings, fixed before submission

Run cold against the contract before deployment:

- **Generic storage construction bug (fixed).** The first draft constructed `self.seals = TreeMap()` etc. in `__init__`. The pinned SDK's generic storage classes are storage-backed descriptors allocated from bare annotations and raise `GenerationError` when constructed directly. Fixed by declaring the maps as bare annotations and assigning only the scalar counters — caught by the direct test suite failing to deploy, then corrected and re-verified green.
- **Replay of `unresolved` rounds (fixed by design, then asserted).** An early rule "one receipt per `(seal, tag)`" would have permanently consumed a slot after a transient network failure. The rule was relaxed so that only a *decision* (`attested`/`rejected`) consumes the slot; `unresolved` keeps it retryable. `test_replay_allowed_after_unresolved` and `test_replay_blocked_after_rejected` pin both halves.
- **Window-length bound (added).** A file-level validation cap of 180 days on `window_end - window_start` was added so a misconfigured seal cannot silently make the `timing` binding nearly meaningless; the test's own window constant was itself the first thing this caught.
- **Policy URL credential check (added).** `https://user:pass@host/...` is rejected, so a seal cannot embed credentials that would be sent to the fetch layer.

## Lint and test evidence

- `genvm-lint check contracts/BoundReleaseReceipt.py` → **Lint passed (3 checks)**, **Validation passed** (7 methods: 5 view, 2 write).
- `gltest tests/direct/test_bound_release_receipt.py` → **40 passed**.

The suite covers the full seal-validation surface, the attested happy path, every binding-driven `rejected` path, both required-fetch `unresolved` paths, the replay rule in both directions, dishonest-leader and leader-error disagreement, body-injection resistance, two tags on one seal, the view error surface, and the pure deterministic helpers.
