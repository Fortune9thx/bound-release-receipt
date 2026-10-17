# Design

This document explains *why* `BoundReleaseReceipt` is built the way it is: the non-determinism boundaries, the storage layout, the threat model, and the limitations that are honestly part of the design rather than oversights.

## 1. The core problem

A release channel is a trust claim: "everything published under `owner/name` within this time window, by these authors, carrying this required asset, complies with this policy document." Verifying that claim *on-chain* means checking real-world sources — the GitHub Releases API and an external policy document — inside a blockchain contract.

A traditional oracle cannot do this without reintroducing trust: a single reporter signs a value and the chain accepts it. GenLayer can, because the chain itself runs the non-deterministic read *independently on every validator* and only writes state when the validators agree. This contract is a minimal, self-contained realization of exactly that.

## 2. Independent re-execution, not leader-trust

The single most important design decision is that **the validator does not trust the leader's result; it recomputes it.**

`attest` wraps one `gl.vm.run_nondet(leader_fn, validator_fn)` call. `leader_fn` does the full job: fetch the GitHub release JSON, fetch the policy document, extract fields, hash the body, compute bindings and the fold, and return `json.dumps(payload, sort_keys=True)`. `validator_fn(leaders_result)` ignores the leader's *content* — it does not parse it, does not check it "looks right" — and instead calls `leader_fn()` itself, producing a second, independent payload from its own two fetches, then returns `own_payload == leaders_payload` as exact string equality.

The consequence is structural, not incidental: a leader cannot fabricate a release, because a fabricated payload only matches if the validator's *own real fetches* happened to produce the identical bytes. A leader that returns a plausible-looking but wrong string is simply disagreed with. `tests/direct/test_bound_release_receipt.py::test_validator_rejects_dishonest_leader` asserts precisely this, and `::test_validator_agrees_with_honest_leader` asserts the affirmative case.

## 3. Why the compared payload is fully deterministic

Everything that crosses the equivalence-principle boundary is derived from the two fetches *without any model*:

- `tag`, `author`: strings read from structured JSON keys (`tag_name`, `author.login`).
- `published_at`: an integer, parsed from GitHub's ISO-8601 `published_at` with integer `timedelta` arithmetic (never floats, never the host timezone).
- `assets`: a **sorted, de-duplicated** list of asset-name strings.
- `body_sha256`, `policy_sha256`: SHA-256 hex digests.
- `bindings`: five plain booleans over the above.
- `fold`: one of three string constants.

There is no free-form generation, no summarization, no classification, no confidence score, and no percentage anywhere. Two honest nodes that observe the same two documents therefore produce byte-identical payloads with certainty, not with high probability — which is what makes strict equality a sound consensus rule rather than a flaky one.

## 4. Non-determinism boundaries

`self.*` storage is touched **only** outside the non-deterministic section:

1. **Deterministic prologue** (before `run_nondet`): load the seal, validate the tag, check the replay index.
2. **Non-deterministic middle** (inside `run_nondet`): `leader_fn`/`validator_fn` fetch over the web. Neither closure references `self`; every value they need was copied into plain locals first (`sealed_repo`, `sealed_policy_url`, `sealed_policy_sha256`, `allowed_authors`, `required_asset`, `window_start`, `window_end`, `sealed_tag`).
3. **Deterministic epilogue** (after `run_nondet` returns): write the receipt and the replay pointer.

This ordering is what prevents the classic bug of reading mutable storage inside a block that runs multiple times (once per validator) — the value read could differ between executions, making the payloads diverge for reasons unrelated to the evidence.

## 5. Storage design

| Field | Type | Purpose |
|---|---|---|
| `seals` | `TreeMap[str, str]` | `seal_id` → normalized, canonical (`sort_keys=True`) seal JSON |
| `receipts` | `TreeMap[str, str]` | `receipt_id` → receipt JSON |
| `tag_to_latest` | `TreeMap[str, str]` | `seal_id + "/" + tag` → latest `receipt_id` |
| `seal_counter` | `u256` | seals opened; next id is `seal-<counter>` |
| `receipt_counter` | `u256` | receipts issued; next id is `receipt-<counter>` |

Everything stored is a JSON string, so the storage encoder never has to handle a nested dataclass or a list-valued field. The replay index key is unambiguous because `seal_id` always matches `seal-<digits>` and a validated tag never contains `/`.

Generic storage classes are allocated by the SDK from their bare annotations; only the scalar counters are assigned in `__init__` (see §10).

## 6. Threat model

| Threat | How it is closed or bounded |
|---|---|
| **Fabricated release accepted because a validator only checked the shape of the leader's claim** | The validator re-runs `leader_fn` itself and compares full payloads byte-for-byte. It never trusts the leader's content. |
| **Prompt injection via the release body or policy text** | The body is hashed and never interpreted; the policy contributes only its SHA-256. The extracted fields come only from their own structured JSON keys. |
| **A required source silently missing yet the round still "passing"** | A failed/non-2xx/unparseable fetch sets the corresponding `*_ok` to `False`; the fold rule then falls through to `unresolved`, and `attested` requires both `*_ok` **and** all bindings `True`. |
| **A policy-hash mismatch being overridden by a model or a leader** | The `policy` binding is `computed_sha256 == sealed_policy_sha256`, computed in pure deterministic code; nothing outside the fetched body can change it. |
| **Replay / double-attesting the same decision** | A `(seal, tag)` slot is consumed permanently by an `attested` or `rejected` receipt; only a prior `unresolved` fold permits retry. |
| **Malformed or hostile caller input** | `open_seal` validates the entire schema and rejects anything off-spec with `gl.vm.UserError`; `attest` validates the tag; unknown ids raise. |
| **Unbounded URLs / cost** | The release URL is built from a validated `owner/name` and a URL-safe tag; the policy URL must be `https://` with a host, no credentials, no whitespace. |
| **Untrusted `sort_keys` serialization drift** | Every stored and compared payload is serialized with `json.dumps(..., sort_keys=True)`, so key order is never a source of divergence. |

## 7. Known, honestly-documented limitations

- **Live-source drift.** Independent re-execution cannot compensate for a source whose *real-world content changes between validators' fetches*. If a release is edited (or a policy document replaced) mid-round, different validators can legitimately observe different documents and disagree, forcing the round toward `unresolved`. This is the correct, fail-closed behavior, but it means `attest` is best-effort with respect to a moving source — pinning the policy via `policy_sha256` is precisely what makes the *policy* half stable.
- **GitHub API rate limits / availability.** A rate-limited or unavailable `api.github.com` yields `unresolved` for that round, not a wrong answer. Callers should retry.
- **The seal freezes a *channel*, not a *codebase*.** The contract attests authorship `submitted_by` for the receipt, but no rule prevents anyone from opening *their own* seal over the same repo with different parameters. A seal is a claim; a receipt is verification of that claim against its stated policy. Consumers should trust a specific `seal_id` they know the parameters of.
- **Tag format is intentionally conservative.** Tags are restricted to `[A-Za-z0-9._-]` starting alphanumeric. Unusual (e.g. slash- or unicode-containing) tags are rejected rather than risk URL ambiguity.

## 8. Integration guide

Two steps, deliberately separate — `attest` is a **write** and must be the caller's own transaction; reading a receipt is a cheap **view**.

**Step 1 — open a seal, then attest a tag (off-chain caller):**

```
open_seal('{"schema":"brr.seal.v1","repo":"owner/name",
           "policy_url":"https://example.com/POLICY.md",
           "policy_sha256":"<64 hex>","allowed_authors":["alice"],
           "required_asset":"provenance.json",
           "window_start":1700000000,"window_end":1701000000}')
attest("seal-0", "v1.2.3")
```

**Step 2 — read the result (any client, or another contract via a view proxy):**

```python
record = json.loads(brr.view().get_receipt("receipt-0"))
assert record["fold"] in ("attested", "rejected", "unresolved")
if record["fold"] == "attested":
    assert record["bindings"] == {"tag": True, "author": True, "timing": True,
                                  "asset": True, "policy": True}
```

Always gate on `record["fold"]`. An `unresolved` receipt is stored but is **not** a positive attestation; retry `attest` for the same `(seal_id, tag)` once the source is reachable.

## 9. Live network observation

The contract was deployed to GenLayer Studio Devnet and exercised with two finalized consensus transactions against a real GitHub release.

- **Network:** GenLayer Studio Devnet (`https://studio-dev.genlayer.com/api`).
- **Contract address:** `0xA85721785d6d3091458CbEAa1555149db322B9ca`.
- **Deploy transaction:** `0x9c3c51c1beb7470a3819347c056d81638e4aefc386ca846bd02e54c815f34203`, settled `Finalized · Accepted`.

The demonstration sealed this repository's own release channel — policy document `docs/POLICY.md`, whose committed blob hashes to `625bc5d672cb90a54822c1040fc6481a5b6dc5f8e4d29a70a3f6fd3df6bdb672`, with `required_asset = provenance.json` and `allowed_authors = ["Fortune9thx"]` — and then attested release `v1.0.0`:

| Step | Transaction | Rounds / outcome |
|---|---|---|
| `open_seal` | `0xcaac665d10b921bef0a343bf596115646c4d20d838ce37a46d442d8b19aa5497` | finalized, `Accepted` |
| `attest(seal-0, v1.0.0)` | `0xe7755776fc07969e60863921ae4a9faef1070955ce063dbec3ad887a724aa10a` | finalized; settlement round 0, `Accepted`, `rotations: 0` |

The settlement record for the `attest` transaction shows `round 0`, `rule: normal`, `outcome: Accepted`, `rotations: 0` — i.e. **the leader and every validator agreed on the first round**, with no rotation and no timeout. This is the expected best case: the release document and the policy document are stable, so independent re-execution converges immediately.

The exact receipt read back with `get_receipt("receipt-0")`:

```json
{
  "bindings": {"asset": true, "author": true, "policy": true, "tag": true, "timing": true},
  "extracted": {
    "assets": ["provenance.json"],
    "author": "Fortune9thx",
    "body_sha256": "0b2c682d6f8b434687df13cba3a96ac16dc5323ad6925d87262fd0cf938fddb2",
    "published_at": 1792214263,
    "tag": "v1.0.0"
  },
  "fold": "attested",
  "policy_ok": true,
  "policy_sha256": "625bc5d672cb90a54822c1040fc6481a5b6dc5f8e4d29a70a3f6fd3df6bdb672",
  "receipt_id": "receipt-0",
  "release_ok": true,
  "repo": "Fortune9thx/bound-release-receipt",
  "schema": "brr.receipt.v1",
  "seal_id": "seal-0",
  "sealed_policy_sha256": "625bc5d672cb90a54822c1040fc6481a5b6dc5f8e4d29a70a3f6fd3df6bdb672",
  "submitted_by": "0xC6E6d3b2acCaECeCeB40Ad4bD3dF123DDCB4e537",
  "tag": "v1.0.0"
}
```

`policy_sha256` (re-derived by consensus) equals `sealed_policy_sha256` (the digest committed into the seal) because both are the SHA-256 of the same committed `docs/POLICY.md` bytes; the `policy` binding is `true` for exactly that reason. No round in this run settled by validator timeout, so nothing negative needs to be reported for it.

## 10. SDK layout notes

The contract retains the canonical header verbatim:

```
# v0.3.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }
from genlayer import *
```

Two additions are required by the pinned runner's actual package layout, and are documented here rather than left as silent surprises:

1. **`import genlayer as gl`.** Under this runner, `from genlayer import *` does not re-export a `gl` name (that alias existed only in the older `genlayer.gl` module layout). The contract therefore imports the package explicitly so `gl.contract`, `gl.vm`, `gl.nondet`, and `gl.public` resolve.
2. **`from genlayer.storage import TreeMap`.** `TreeMap` is defined under `genlayer.storage` in this layout and is imported explicitly for the storage annotations.

Additionally, in this SDK layout the generic storage classes are **storage-backed descriptors that the framework allocates from their bare annotations**; constructing one directly (`self.seals = TreeMap()`) raises `GenerationError: generic storage classes can not be instantiated with __init__`. The contract therefore declares the three `TreeMap` fields as bare annotations and assigns only the two scalar `u256` counters in `__init__`.

These are import/idiom adjustments only — the contract's logic is independent of them, and `genvm-lint check` plus `genvm-lint validate` both pass against the pinned runner.
