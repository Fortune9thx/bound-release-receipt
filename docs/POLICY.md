# BoundReleaseReceipt Release Policy

This document is the **policy document** for the `bound-release-receipt`
release channel. Its SHA-256 digest is embedded in each seal as
`policy_sha256`, so this file is content-addressed: any change to a single byte
produces a different digest and therefore a different seal.

The `BoundReleaseReceipt` contract binds three things together for every release
tag that is submitted for attestation:

1. **Authorship** — the release's author login must appear in the seal's
   `allowed_authors` list.
2. **Timing** — the release's `published_at` timestamp must fall inside the
   seal's closed window `[window_start, window_end]`.
3. **Required build asset** — the release must carry an asset whose name equals
   the seal's `required_asset`.

## What a receipt means

A receipt is issued only when every validator independently re-fetched the
GitHub release document for the requested tag, re-extracted the same canonical
fields, and recomputed every binding below:

- `tag` — the release `tag_name` equals the requested tag.
- `author` — the release `author.login` is listed in `allowed_authors`.
- `timing` — `published_at` is within the sealed window.
- `asset` — `required_asset` is present in the release's asset names.
- `policy` — the fetched policy body hashes to the sealed `policy_sha256`.

The fold is:

- `attested` — both fetches succeeded and every binding is `true`.
- `rejected` — both fetches succeeded but at least one binding is `false`.
- `unresolved` — one or both required fetches failed, so no decision is made.

Only `attested` is a positive attestation. `unresolved` is a non-outcome: it
does not consume the `(seal, tag)` slot and may be retried.

## Required asset

The canonical release asset for this channel is:

```
provenance.json
```

A release must attach a file with exactly this name. The contract never reads
the asset's contents; it only requires that an asset with this name exists.

## Allowed authors

Releases on this channel must be authored by a login in the seal's
`allowed_authors` list. For the reference seal used in this repository's live
demonstration, the sole allowed author is `Fortune9thx`.

## Release window

Each seal declares a closed window of at most 180 days. Releases published
outside that window cannot be attested by that seal. A new seal may be opened at
any time to cover a different window.

## Scope and invariants

- The release body (release notes prose) is hashed and never interpreted. No
  model reads the prose, so prose cannot influence any binding.
- The policy digest and every other binding are compared in pure deterministic
  code after extraction; no leader claim or model output can override them.
- Storage is limited to JSON strings in `TreeMap`s plus two integer counters;
  the contract holds no funds and grants no authority.
