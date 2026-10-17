# Changelog

All notable changes to this project are documented in this file.

## [1.0.0] - 2026-10-17

Initial release.

- `open_seal(seal_json) -> str` — deterministic validation and persistence of a `brr.seal.v1` release-channel seal (repo, policy URL + SHA-256, allowed authors, required asset, time window).
- `attest(seal_id, tag) -> str` — independent re-fetch and re-extraction of both the GitHub release JSON and the policy document by every validator, with a fully discrete consensus payload and a `rejected`/`attested`/`unresolved` fold.
- `get_seal`, `get_receipt`, `get_latest_for_tag`, `get_seal_count`, `get_receipt_count` view methods.
- Replay rule: a `(seal, tag)` slot is consumed permanently by an `attested` or `rejected` receipt, and stays retryable after an `unresolved` one.
- Body-hash-only treatment of the release body, so prompt injection in release prose cannot influence any extracted field or binding.
- 40 direct-mode tests; `genvm-lint check` and `genvm-lint validate` both clean.
- CI workflow running lint, validation, and tests on every push.

### Fixed during development (before the 1.0.0 tag)

- Replaced direct `TreeMap()` construction in `__init__` with bare-annotation storage fields plus scalar-counter initialization, matching the pinned SDK's storage descriptor model.
- Relaxed the replay rule from "one receipt per `(seal, tag)`" to "one *decision* per `(seal, tag)`" so a transient `unresolved` round does not permanently consume the slot.
- Added the 180-day maximum window-span validation and the policy-URL credential check.
