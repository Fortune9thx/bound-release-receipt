# BoundReleaseReceipt

A standalone [GenLayer](https://genlayer.com) Intelligent Contract primitive that **seals a release channel and issues a release receipt only when every validator independently re-fetches and re-extracts both the live GitHub release and the sealed policy document, and every deterministic binding holds.**

It answers one narrow, checkable question — *"is this exact tag a valid release from this sealed channel, right now?"* — without any model judgment, any confidence percentage, or any leader-trust shortcut.

## The trust problem it closes

A contract that wants to attest "this release happened, and it complied with this policy" has two ways to get it wrong:

1. **Validator only checks the shape of the leader's answer.** If a validator accepts a well-formed JSON blob without re-fetching the source itself, the leader can fabricate any release. This contract's validator never does that — it calls `leader_fn()` itself (a completely fresh fetch and extraction of *both* sources) and compares its own normalized payload byte-for-byte against the leader's.
2. **A quantitative outcome not exactly agreed on.** A score, a confidence band, a "mostly-compliant" tier — anything like that makes two honest nodes diverge. This contract compares only strings, one integer, one sorted list, and two SHA-256 digests, and reduces them to a fold that is ordinary deterministic Python. Identical observations always produce an identical payload.

## What it does

A caller first **opens a seal** (`open_seal`), describing a release channel:

| Seal field | Meaning |
|---|---|
| `schema` | must be `"brr.seal.v1"` |
| `repo` | `owner/name`, `[A-Za-z0-9_.-]` only |
| `policy_url` | `https://` URL of the release policy document, no embedded credentials |
| `policy_sha256` | exactly 64 lowercase hex — the expected SHA-256 of that document |
| `allowed_authors` | 1–20 GitHub logins, each ≤ 39 chars |
| `required_asset` | a build asset (by exact filename) that must be attached |
| `window_start` / `window_end` | unix seconds; `window_end > window_start`; span ≤ 180 days |

Then a caller calls **`attest(seal_id, tag)`**. Every node — leader *and* every validator, independently — re-fetches the GitHub release JSON for that tag and the policy document from scratch, extracts the same canonical fields, and recomputes the same deterministic bindings. Only a payload every validator independently reproduces is accepted, and the receipt records the resulting fold.

### The fold

The fold is a pure function of two booleans and five bindings. No model, no partial credit.

| Condition | Fold |
|---|---|
| both sources fetched OK, all five bindings `True` | `attested` |
| both sources fetched OK, at least one binding `False` | `rejected` |
| either source failed to fetch (network, non-2xx, unparseable/missing field) | `unresolved` |

`unresolved` is deliberately *not* a decision: a receipt is still stored (so the caller can inspect what failed), but the `(seal, tag)` slot stays retryable. An `attested` or `rejected` fold consumes the slot permanently.

### The bindings

| Binding | Computed as |
|---|---|
| `tag` | fetched `tag_name` == requested tag |
| `author` | fetched `author.login` ∈ `allowed_authors` |
| `timing` | `window_start ≤ published_at ≤ window_end` |
| `asset` | `required_asset` present in the fetched asset-name list |
| `policy` | fetched policy body SHA-256 == sealed `policy_sha256` |

The release **body** is hashed and never interpreted — its SHA-256 is the only thing it contributes, so prompt-injection text in a release body cannot change the tag, author, assets, or any binding.

## Contract interface

| Method | Kind | Description |
|---|---|---|
| `open_seal(seal_json: str) -> str` | write | Validate and persist a seal; returns its `seal_id` (`seal-0`, `seal-1`, …) |
| `attest(seal_id: str, tag: str) -> str` | write | Re-fetch both sources under independent re-execution consensus and issue a receipt; returns its `receipt_id` |
| `get_seal(seal_id: str) -> str` | view | Normalized seal JSON |
| `get_receipt(receipt_id: str) -> str` | view | Full receipt JSON (fold, extracted fields, bindings, `submitted_by`) |
| `get_latest_for_tag(seal_id: str, tag: str) -> str` | view | `receipt_id` of the most recent receipt for `(seal_id, tag)` — read its `fold` |
| `get_seal_count() -> u256` | view | Total seals ever opened |
| `get_receipt_count() -> u256` | view | Total receipts ever issued |

## How consensus actually works here

```mermaid
sequenceDiagram
    participant Caller
    participant Leader
    participant Validators as Every Validator (independently)
    participant Chain

    Caller->>Leader: attest(seal_id, tag)
    Leader->>Leader: deterministic: load seal, check replay slot
    Leader->>Leader: leader_fn(): fetch release JSON + policy doc
    Leader->>Leader: extract fields, hash body, compute bindings + fold
    Leader-->>Chain: propose json.dumps(payload, sort_keys=True)

    Chain->>Validators: run validator_fn(leader_return)
    Validators->>Validators: call leader_fn() itself (fresh fetch + extract)
    Validators-->>Chain: True only if own payload == leader's, byte-for-byte

    alt every validator agreed
        Chain->>Chain: write receipts[id] + tag_to_latest[(seal, tag)]
        Chain-->>Caller: receipt_id
    end
```

Because `leader_fn` and `validator_fn` each do their own fetch and their own extraction, and because the validator's only input from the leader is the payload *string* it compares against its own independently derived string, there is no code path where a well-formed but fabricated leader payload is accepted.

## Repository layout

```
contracts/BoundReleaseReceipt.py              the contract
tests/direct/                                 gltest direct-mode test suite (40 tests)
docs/DESIGN.md                                design rationale, non-determinism boundaries, threat model
docs/WHY_THIS_PASSES_REVIEW.md                mapping against known Portal rejection patterns
PORTAL_SUBMISSION.md                          submission text for the GenLayer Portal
SECURITY.md                                   scope and vulnerability reporting
CHANGELOG.md                                  release history
.github/workflows/ci.yml                      lint + validation + direct tests on every push
```

## Testing

The suite runs the contract in gltest's direct mode with mocked web responses — no network, no live node required.

```bash
pip install -r requirements-test.txt
# Pin the genvm-manager release whose bundle ships the runner named in the header:
export GENVM_VERSION=v0.6.0-rc8          # PowerShell: $env:GENVM_VERSION="v0.6.0-rc8"
genvm-lint check contracts/BoundReleaseReceipt.py
gltest tests/direct/test_bound_release_receipt.py -v
```

40 direct-mode tests cover: the full seal-validation surface (schema, repo, sha, author list, window, policy URL), the attested happy path, each binding-driven `rejected` path (author, asset, timing, policy-hash, tag), both required-fetch failures (`unresolved`), the replay rule (blocked after a decision, allowed after `unresolved`), a dishonest-leader disagreement and a leader-error disagreement, body-injection resistance, two independent tags on one seal, the view error surface, and the pure deterministic helpers. `genvm-lint check` passes with zero warnings and `genvm-lint validate` passes semantic validation.

## A note on the SDK header

The contract keeps the canonical GenLayer header exactly:

```
# v0.3.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }
from genlayer import *
```

Under the pinned runner, `from genlayer import *` no longer re-exports the `gl` namespace (that alias existed only in the older `genlayer.gl` layout), so the contract adds `import genlayer as gl` and imports `TreeMap` from `genlayer.storage` explicitly. See [docs/DESIGN.md](docs/DESIGN.md#10-sdk-layout-notes) for the exact reasoning, and `Storage` below.

## Storage

All state is `TreeMap[str, str]` of JSON strings plus two `u256` counters:

| Field | Type | Purpose |
|---|---|---|
| `seals` | `TreeMap[str, str]` | `seal_id` → normalized seal JSON |
| `receipts` | `TreeMap[str, str]` | `receipt_id` → receipt JSON |
| `tag_to_latest` | `TreeMap[str, str]` | `seal_id + "/" + tag` → latest `receipt_id` |
| `seal_counter` | `u256` | seals opened |
| `receipt_counter` | `u256` | receipts issued |

Generic storage classes are allocated from their bare annotations by the SDK; only the two scalar counters are assigned in `__init__`. There are no funds, no reputation, no scores, and no privileged roles anywhere in the contract.

## License

[MIT](LICENSE)
