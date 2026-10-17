# Portal Submission

## Project name

BoundReleaseReceipt

## Category

Intelligent Contract primitive / on-chain release verification

## One-line pitch

A GenLayer Intelligent Contract that seals a GitHub release channel and issues a receipt only when every validator independently re-fetches and re-extracts the release and the sealed policy and all deterministic bindings hold.

## Description / Notes

BoundReleaseReceipt seals a release channel — repo, policy document + its SHA-256, allowed authors, a required build asset, and a time window — and then issues a release receipt only under full independent re-execution. To attest a tag, the leader and every validator each fetch the GitHub release JSON and the policy document from scratch, extract the same fields, hash the release body, compute five deterministic bindings (tag, author, timing, asset, policy-hash), and derive a fold. A validator never trusts the leader's content: it re-runs the leader function itself and compares the full normalized payload byte-for-byte. There is no model, no confidence score, no percentage, and no partial credit anywhere in the compared payload. If either required fetch fails the fold is `unresolved` (never `attested`), and only a decision consumes the `(seal, tag)` slot, so transient failures stay retryable. The release body is hashed only and never interpreted, so injection in release prose cannot change any extracted field.

## Deployed contract

- Network: GenLayer Studio Devnet (Studionet)
- Address: _pending live deployment_
- Deploy transaction: _pending_
- Final status: _pending_

## Repository

https://github.com/Fortune9thx/bound-release-receipt

## Key features

- **Independent re-execution, not leader-trust** — the validator recomputes the entire payload itself, including both fetches.
- **Fully deterministic consensus payload** — strings, one integer, one sorted list, two SHA-256 digests, five booleans, one fold string. No floats, no model.
- **Fail-closed required fetches** — any fetch failure yields `unresolved`, and `attested` is structurally impossible without both sources and all bindings.
- **Injection-proof** — no fetched content is ever given to a model; the release body contributes only its hash.
- **Retry-safe** — only `attested`/`rejected` consume the `(seal, tag)` slot.
- **No privileged roles, no funds, no scores.**

## Evidence of quality

- `genvm-lint check contracts/BoundReleaseReceipt.py` → Lint passed (3 checks), Validation passed (7 methods: 5 view, 2 write).
- `gltest tests/direct/test_bound_release_receipt.py` → 40 passed.
- Design rationale, non-determinism boundaries, and threat model in `docs/DESIGN.md`.
- Rejection-pattern mapping in `docs/WHY_THIS_PASSES_REVIEW.md`.

## Links

- Repository: https://github.com/Fortune9thx/bound-release-receipt
- Contract header pins `py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng` (SDK v0.3.0).
