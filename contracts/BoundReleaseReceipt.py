# v0.3.0
# { "Depends": "py-genlayer:5jycge4q8k23462jtb0b9fyey1s9qz928sz2nbrd9mg4sxqg2qng" }

"""
BoundReleaseReceipt -- a standalone GenLayer Intelligent Contract that seals a
release channel and issues a release receipt only when the sealed policy and the
live GitHub release are both independently re-fetched and re-extracted by every
validator, and every deterministic binding holds.

------------------------------------------------------------------------------
WHAT THIS CONTRACT DOES
------------------------------------------------------------------------------
A caller opens a "seal" (`open_seal`) describing a release channel:

    {
      "schema": "brr.seal.v1",
      "repo": "owner/name",
      "policy_url": "https://...",          # a release policy document
      "policy_sha256": "<64 lowercase hex>",# expected SHA-256 of that document
      "allowed_authors": ["login1", ...],   # up to 20 GitHub logins
      "required_asset": "provenance.json",  # a build asset that must be attached
      "window_start": <unix seconds>,
      "window_end": <unix seconds>
    }

Later, a caller calls `attest(seal_id, tag)`. Every node in the validator set --
the leader AND every validator, independently -- re-fetches the GitHub release
JSON for that tag and the policy document from scratch, extracts the same small
set of canonical fields, and recomputes the same deterministic binding checks.
Only a payload that every validator independently reproduces byte-for-byte is
accepted, and the resulting receipt records the fold ("attested", "rejected", or
"unresolved") together with the extracted fields.

------------------------------------------------------------------------------
WHY VALIDATORS RE-FETCH AND RE-EXTRACT (the non-determinism boundary)
------------------------------------------------------------------------------
The only externally-influenced operation in this contract is `attest`, and the
only non-deterministic call inside it is the single `gl.vm.run_nondet(
leader_fn, validator_fn)` call. `leader_fn` and `validator_fn` are the ONLY
places that touch `gl.nondet.web.get`. They never read or write `self.*`
storage; they close only over plain local values copied out of the seal before
the non-deterministic section begins.

`leader_fn` and `validator_fn` each perform their own fetch of BOTH sources and
their own extraction. A validator does not trust the leader's claim: it calls
`leader_fn()` itself (a completely fresh fetch + extraction) and compares the
full normalized JSON payload. There is therefore no code path where a validator
accepts a receipt because the leader returned a well-formed string or a
plausible JSON shape -- the payload can only match if the validator observed the
same real-world data and derived the same booleans.

The "no storage reads or writes inside any non-deterministic block" rule is
upheld structurally: `attest` reads the seal and the replay index deterministically
BEFORE `run_nondet`, and it writes the receipt deterministically AFTER
`run_nondet` returns. `leader_fn`/`validator_fn` have no reference to `self`.

------------------------------------------------------------------------------
WHY THE FOLD IS DETERMINISTIC AFTER THE EXTRACTS
------------------------------------------------------------------------------
The fold is a pure function of two booleans (`release_ok`, `policy_ok`) and five
boolean bindings, all computed from already-extracted, already-normalized values
(strings, one integer, one sorted list, two hex digests). There is no model
judgment, no confidence band, no percentage, and no partial credit anywhere in
the compared payload. Once the extracts exist, the fold is ordinary deterministic
Python: identical inputs on every node produce an identical fold, and hence an
identical JSON string. This is what lets strict whole-payload equality actually
converge across independently-run nodes.

------------------------------------------------------------------------------
WHY THE MODEL NEVER JUDGES RELEASE-NOTE PROSE
------------------------------------------------------------------------------
This contract does not summarize, classify, or otherwise interpret the release
body or any free-form text. The release body is reduced to a single SHA-256 hash
and is never fed to any model. The extracted fields (tag, author login,
published time, asset-name list) come only from structured JSON keys. Nothing
about the *prose* of a release can change the extracted fields, the bindings, or
the fold.

------------------------------------------------------------------------------
WHY A FAILED REQUIRED FETCH CANNOT PRODUCE "attested"
------------------------------------------------------------------------------
If either fetch fails -- network error, non-2xx status, or an unparseable/missing
required field -- the corresponding `*_ok` flag is `False`, and the fold rule
falls through to "unresolved". "attested" requires `release_ok and policy_ok` AND
every binding `True`, so a missing or failed required fetch can never yield a
positive receipt.

------------------------------------------------------------------------------
WHY A POLICY-HASH MISMATCH CANNOT BE OVERRIDDEN
------------------------------------------------------------------------------
The sealed `policy_sha256` is a constant captured into the leader/validator
closures before the non-deterministic section. The `policy` binding is computed
as `computed_sha256 == sealed_policy_sha256` in pure deterministic code. No model
output, no leader claim, and no validator input can change that comparison: the
only way to make `policy` True is for the fetched policy body to hash to exactly
the sealed digest.

------------------------------------------------------------------------------
WHY BODY INJECTION CANNOT CHANGE EXTRACTED FIELDS
------------------------------------------------------------------------------
The release `body` is read once, encoded to UTF-8, and hashed; the hash is the
only thing that body ever contributes. Prompt-injection-style instructions placed
inside the release body or the release notes have no channel to influence `tag`,
`author`, `published_at`, or `assets`, because those are read from their own JSON
keys and are compared against the sealed values deterministically. This contract
never executes instructions found in fetched content.

------------------------------------------------------------------------------
STORAGE
------------------------------------------------------------------------------
All storage is `TreeMap[str, str]` of JSON strings plus two `u256` counters.
No funds, no reputation, no scores, no partial credit, and no consent/ratification
object exist in this contract. Views return `str` (or `u256` for counters).

------------------------------------------------------------------------------
SDK IMPORT NOTE
------------------------------------------------------------------------------
The original GenLayer header `from genlayer import *` is preserved exactly. Under
the pinned v0.3.0 runner (`py-genlayer:5jycge4q...`) the package no longer
re-exports the `gl` namespace from a wildcard import (that alias existed in the
older `genlayer.gl` layout), so `import genlayer as gl` is added explicitly to
keep `gl.contract`, `gl.vm`, `gl.nondet`, and `gl.public` available. `TreeMap`
lives in `genlayer.storage` in this SDK layout and is imported explicitly.
"""

from genlayer import *
import genlayer as gl
from genlayer.storage import TreeMap
import json
import typing
import hashlib
import re
import datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEAL_SCHEMA = "brr.seal.v1"
RECEIPT_SCHEMA = "brr.receipt.v1"

MIN_ALLOWED_AUTHORS = 1
MAX_ALLOWED_AUTHORS = 20
MAX_AUTHOR_LOGIN_CHARS = 39  # GitHub's own maximum login length
MAX_REQUIRED_ASSET_CHARS = 128
MAX_TAG_CHARS = 128

# A window longer than 180 days is almost certainly a misconfigured seal, and a
# very wide window weakens the point of binding `published_at` at all.
MAX_WINDOW_SECONDS = 180 * 24 * 60 * 60

# `owner/name`. Intentionally permissive within each segment but strict about the
# single slash: this string is interpolated directly into an api.github.com URL,
# so no character that could alter the URL's structure (a second slash, a `?`,
# a `#`, whitespace, control characters) is permitted.
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

# A git tag, kept to a conservative URL-safe alphabet. Leading character must be
# alphanumeric so a tag can never be empty, start with a separator, or be a bare
# `-`/`.` that could confuse URL or filesystem handling downstream.
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0," + str(MAX_TAG_CHARS - 1) + r"}")

# Exactly 64 lowercase hexadecimal characters.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

ALLOWED_FOLDS = ("attested", "rejected", "unresolved")


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


class BoundReleaseReceipt(gl.contract.Contract):
    seals: TreeMap[str, str]
    receipts: TreeMap[str, str]
    tag_to_latest: TreeMap[str, str]
    seal_counter: u256
    receipt_counter: u256

    def __init__(self):
        # Only the two scalar counters are assigned here. In this SDK layout the
        # generic storage classes (TreeMap) are storage-backed descriptors that
        # the framework allocates from their bare annotations; constructing one
        # directly (`self.seals = TreeMap()`) is forbidden and raises
        # GenerationError. Their storage slots therefore exist and read as empty
        # from the first block onward without an explicit assignment.
        self.seal_counter = u256(0)
        self.receipt_counter = u256(0)

    # -----------------------------------------------------------------
    # Public write: open a release-channel seal
    # -----------------------------------------------------------------
    @gl.public.write
    def open_seal(self, seal_json: str) -> str:
        """Validate and persist a release-channel seal, returning its seal_id.

        This method is fully deterministic: it performs no fetch and no
        non-deterministic call. It only validates caller-supplied JSON against a
        fixed schema and stores the normalized result. Every field validated
        here is later re-read (deterministically) by `attest` and captured into
        the leader/validator closures, so a seal can never be mutated after the
        fact -- the binding values live inside an immutable receipt once issued.
        """
        normalized = _validate_seal(seal_json)

        seal_id = "seal-" + str(int(self.seal_counter))
        self.seal_counter = u256(int(self.seal_counter) + 1)
        self.seals[seal_id] = json.dumps(normalized, sort_keys=True)
        return seal_id

    # -----------------------------------------------------------------
    # Public write: attest a release tag against a seal
    # -----------------------------------------------------------------
    @gl.public.write
    def attest(self, seal_id: str, tag: str) -> str:
        """Re-fetch the sealed release and policy, and issue a receipt.

        The deterministic prologue (seal load, replay check) reads storage.
        The non-deterministic middle contains exactly ONE `run_nondet` call. The
        deterministic epilogue writes storage. No storage access happens inside
        `leader_fn`/`validator_fn`, and neither closure holds a reference to
        `self`.
        """
        # ---- Deterministic prologue: load and validate the seal ----
        raw_seal = self.seals.get(seal_id)
        if raw_seal is None:
            raise gl.vm.UserError("unknown seal_id: " + seal_id)
        try:
            seal = json.loads(raw_seal)
        except (ValueError, TypeError):
            raise gl.vm.UserError("stored seal is not valid JSON")

        requested_tag = _validate_tag(tag)

        # One receipt per (seal, tag) -- unless the previous fold was unresolved.
        # An unresolved round is a non-outcome (a required fetch failed), so it
        # must not permanently consume the slot; a rejected or attested round is
        # a decision and does consume it.
        replay_key = _tag_key(seal_id, requested_tag)
        previous_receipt_id = self.tag_to_latest.get(replay_key)
        if previous_receipt_id is not None:
            raw_previous = self.receipts.get(previous_receipt_id)
            if raw_previous is not None:
                try:
                    previous_fold = json.loads(raw_previous).get("fold")
                except (ValueError, TypeError):
                    previous_fold = None
                if previous_fold != "unresolved":
                    raise gl.vm.UserError(
                        "a receipt already exists for this (seal, tag) with fold="
                        + str(previous_fold)
                    )

        # Copy every binding value into plain locals. The closures below close
        # ONLY over these locals (never over `self`), which is what keeps the
        # non-deterministic section free of storage access and keeps the closures
        # cloudpickle-serializable for the validator sandbox.
        sealed_repo = seal["repo"]
        sealed_policy_url = seal["policy_url"]
        sealed_policy_sha256 = seal["policy_sha256"]
        allowed_authors = list(seal["allowed_authors"])
        required_asset = seal["required_asset"]
        window_start = int(seal["window_start"])
        window_end = int(seal["window_end"])
        sealed_tag = requested_tag

        # -----------------------------------------------------------------
        # NON-DETERMINISTIC SECTION. Exactly one `run_nondet` call. Every
        # gl.nondet.web.get lives inside these two closures. Neither closure
        # reads or writes self.*.
        # -----------------------------------------------------------------
        def leader_fn() -> str:
            """Freshly fetch BOTH sources and derive the canonical payload."""
            # --- Source 1: the GitHub release JSON for the requested tag ---
            release_ok = False
            release = {
                "tag": "",
                "author": "",
                "published_at": 0,
                "assets": [],
                "body_sha256": "",
            }
            release_url = (
                "https://api.github.com/repos/"
                + sealed_repo
                + "/releases/tags/"
                + sealed_tag
            )
            try:
                release_resp = gl.nondet.web.get(
                    release_url,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "bound-release-receipt",
                    },
                )
                if 200 <= release_resp.status < 300 and release_resp.body is not None:
                    # Extraction reads ONLY structured JSON keys. The free-form
                    # body is hashed here and never interpreted.
                    release = _extract_release(release_resp.body)
                    release_ok = True
            except Exception:
                # A failed required fetch yields release_ok=False, which forces
                # the fold to "unresolved" below -- never "attested".
                release_ok = False
                release = {
                    "tag": "",
                    "author": "",
                    "published_at": 0,
                    "assets": [],
                    "body_sha256": "",
                }

            # --- Source 2: the sealed policy document ---
            policy_ok = False
            computed_policy_sha256 = ""
            try:
                policy_resp = gl.nondet.web.get(
                    sealed_policy_url,
                    headers={"User-Agent": "bound-release-receipt"},
                )
                if 200 <= policy_resp.status < 300 and policy_resp.body is not None:
                    computed_policy_sha256 = hashlib.sha256(
                        bytes(policy_resp.body)
                    ).hexdigest()
                    policy_ok = True
            except Exception:
                policy_ok = False
                computed_policy_sha256 = ""

            # --- Deterministic bindings, computed after the extracts ---
            # Every binding is a plain boolean over already-extracted values.
            # `policy` compares the computed digest against the sealed digest;
            # that comparison is the ONLY thing that can set it True.
            bindings = {
                "tag": release_ok and release["tag"] == sealed_tag,
                "author": release_ok and release["author"] in allowed_authors,
                "timing": release_ok
                and (window_start <= release["published_at"] <= window_end),
                "asset": release_ok and (required_asset in release["assets"]),
                "policy": release_ok
                and policy_ok
                and (computed_policy_sha256 == sealed_policy_sha256),
            }

            # --- The fold is a pure function of the two *_ok flags + bindings ---
            if release_ok and policy_ok:
                fold = "attested" if all(bindings.values()) else "rejected"
            else:
                fold = "unresolved"

            payload = {
                "release_ok": release_ok,
                "policy_ok": policy_ok,
                "tag": release["tag"],
                "author": release["author"],
                "published_at": release["published_at"],
                "assets": release["assets"],
                "body_sha256": release["body_sha256"],
                "policy_sha256": computed_policy_sha256,
                "bindings": bindings,
                "fold": fold,
            }
            return json.dumps(payload, sort_keys=True)

        def validator_fn(leaders_result) -> bool:
            """Independently reproduce the leader's full payload and compare.

            Returns True only if the leader returned a *string* payload AND this
            validator's own fresh fetch + extraction produced a byte-for-byte
            identical normalized payload. Any exception is treated as disagreement.
            """
            if not isinstance(leaders_result, gl.vm.Return):
                return False
            try:
                claimed = leaders_result.calldata
            except Exception:
                return False
            if not isinstance(claimed, str):
                return False
            try:
                independently_derived = leader_fn()
            except Exception:
                return False
            return independently_derived == claimed

        payload_json = gl.vm.run_nondet(leader_fn, validator_fn)

        # -----------------------------------------------------------------
        # DETERMINISTIC EPILOGUE. Reached only after consensus. Plain state
        # mutation, identical on every node.
        # -----------------------------------------------------------------
        if not isinstance(payload_json, str):
            raise gl.vm.UserError("consensus returned a non-string payload")
        try:
            payload = json.loads(payload_json)
        except (ValueError, TypeError):
            raise gl.vm.UserError("consensus payload was not valid JSON")
        if not isinstance(payload, dict):
            raise gl.vm.UserError("consensus payload was not a JSON object")

        fold = payload.get("fold")
        if fold not in ALLOWED_FOLDS:
            raise gl.vm.UserError("consensus returned an unknown fold")

        receipt_id = "receipt-" + str(int(self.receipt_counter))
        self.receipt_counter = u256(int(self.receipt_counter) + 1)

        record = {
            "schema": RECEIPT_SCHEMA,
            "receipt_id": receipt_id,
            "seal_id": seal_id,
            "tag": requested_tag,
            "repo": sealed_repo,
            "fold": fold,
            # "attested" is the only fold that is ever a positive attestation.
            # "unresolved" is stored so the slot stays retryable, but callers
            # must gate on `fold` and must never treat it as attested.
            "release_ok": _as_bool(payload.get("release_ok")),
            "policy_ok": _as_bool(payload.get("policy_ok")),
            "extracted": {
                "tag": _as_str(payload.get("tag")),
                "author": _as_str(payload.get("author")),
                "published_at": _as_int(payload.get("published_at")),
                "assets": _as_str_list(payload.get("assets")),
                "body_sha256": _as_str(payload.get("body_sha256")),
            },
            "policy_sha256": _as_str(payload.get("policy_sha256")),
            "sealed_policy_sha256": sealed_policy_sha256,
            "bindings": _as_binding_map(payload.get("bindings")),
            "submitted_by": str(gl.message.sender_address),
        }

        self.receipts[receipt_id] = json.dumps(record, sort_keys=True)
        self.tag_to_latest[replay_key] = receipt_id
        return receipt_id

    # -----------------------------------------------------------------
    # Public views
    # -----------------------------------------------------------------
    @gl.public.view
    def get_seal(self, seal_id: str) -> str:
        """Return the normalized seal JSON, or raise if unknown."""
        raw = self.seals.get(seal_id)
        if raw is None:
            raise gl.vm.UserError("no seal found for id: " + seal_id)
        return raw

    @gl.public.view
    def get_receipt(self, receipt_id: str) -> str:
        """Return the full receipt JSON, or raise if unknown."""
        raw = self.receipts.get(receipt_id)
        if raw is None:
            raise gl.vm.UserError("no receipt found for id: " + receipt_id)
        return raw

    @gl.public.view
    def get_latest_for_tag(self, seal_id: str, tag: str) -> str:
        """Return the receipt_id of the most recent receipt for (seal_id, tag).

        The returned receipt may have any fold, including "unresolved" -- callers
        must read the receipt's `fold` field and must only treat "attested" as a
        positive attestation.
        """
        receipt_id = self.tag_to_latest.get(_tag_key(seal_id, tag))
        if receipt_id is None:
            raise gl.vm.UserError("no receipt for this (seal, tag)")
        return receipt_id

    @gl.public.view
    def get_seal_count(self) -> u256:
        return self.seal_counter

    @gl.public.view
    def get_receipt_count(self) -> u256:
        return self.receipt_counter


# ---------------------------------------------------------------------------
# Module-level deterministic helpers
#
# These are pure functions -- no fetch, no model, no storage. They are used both
# in the deterministic validation path and inside the leader/validator closures.
# Because they are module-level (not methods and not closures over `self`), the
# closures that call them stay free of any contract-instance reference.
# ---------------------------------------------------------------------------


def _validate_seal(seal_json: str) -> dict:
    """Validate `seal_json` against the brr.seal.v1 schema and normalize it.

    Raises gl.vm.UserError on any violation. Returns a dict with exactly the
    sealed fields, suitable for canonical JSON serialization.
    """
    if not isinstance(seal_json, str):
        raise gl.vm.UserError("seal_json must be a string")
    try:
        doc = json.loads(seal_json)
    except (ValueError, TypeError):
        raise gl.vm.UserError("seal_json is not valid JSON")
    if not isinstance(doc, dict):
        raise gl.vm.UserError("seal must be a JSON object")

    if doc.get("schema") != SEAL_SCHEMA:
        raise gl.vm.UserError("seal.schema must be " + SEAL_SCHEMA)

    repo = doc.get("repo")
    if not isinstance(repo, str) or _REPO_RE.fullmatch(repo) is None:
        raise gl.vm.UserError(
            "seal.repo must match owner/name using [A-Za-z0-9_.-] only"
        )

    policy_url = _validate_policy_url(doc.get("policy_url"))

    policy_sha256 = doc.get("policy_sha256")
    if not isinstance(policy_sha256, str) or _SHA256_RE.fullmatch(policy_sha256) is None:
        raise gl.vm.UserError(
            "seal.policy_sha256 must be exactly 64 lowercase hex characters"
        )

    raw_authors = doc.get("allowed_authors")
    if not isinstance(raw_authors, list):
        raise gl.vm.UserError("seal.allowed_authors must be a list")
    if not (MIN_ALLOWED_AUTHORS <= len(raw_authors) <= MAX_ALLOWED_AUTHORS):
        raise gl.vm.UserError(
            "seal.allowed_authors must contain "
            + str(MIN_ALLOWED_AUTHORS)
            + "-"
            + str(MAX_ALLOWED_AUTHORS)
            + " entries"
        )
    allowed_authors = []
    for author in raw_authors:
        if not isinstance(author, str) or not author:
            raise gl.vm.UserError("each allowed author must be a non-empty string")
        if len(author) > MAX_AUTHOR_LOGIN_CHARS:
            raise gl.vm.UserError(
                "each allowed author must be at most "
                + str(MAX_AUTHOR_LOGIN_CHARS)
                + " characters"
            )
        allowed_authors.append(author)

    required_asset = doc.get("required_asset")
    if (
        not isinstance(required_asset, str)
        or not required_asset
        or len(required_asset) > MAX_REQUIRED_ASSET_CHARS
    ):
        raise gl.vm.UserError(
            "seal.required_asset must be a non-empty string of at most "
            + str(MAX_REQUIRED_ASSET_CHARS)
            + " characters"
        )

    window_start = doc.get("window_start")
    window_end = doc.get("window_end")
    if not _is_plain_int(window_start) or not _is_plain_int(window_end):
        raise gl.vm.UserError("seal.window_start and seal.window_end must be integers")
    window_start = int(window_start)
    window_end = int(window_end)
    if window_start < 0 or window_end < 0:
        raise gl.vm.UserError("seal.window_start and seal.window_end must be >= 0")
    if window_end <= window_start:
        raise gl.vm.UserError("seal.window_end must be greater than seal.window_start")
    if (window_end - window_start) > MAX_WINDOW_SECONDS:
        raise gl.vm.UserError(
            "seal window must be at most 180 days (" + str(MAX_WINDOW_SECONDS) + "s)"
        )

    return {
        "schema": SEAL_SCHEMA,
        "repo": repo,
        "policy_url": policy_url,
        "policy_sha256": policy_sha256,
        "allowed_authors": allowed_authors,
        "required_asset": required_asset,
        "window_start": window_start,
        "window_end": window_end,
    }


def _validate_policy_url(url: typing.Any) -> str:
    """Validate a policy URL: https, a host, and no embedded credentials.

    Deliberately check-based rather than using urllib: only the authority
    component is inspected for a userinfo (`@`) and for emptiness, so schemes,
    paths, and queries keep full expressiveness while credential embedding and
    hostless URLs are rejected.
    """
    if not isinstance(url, str) or not url.startswith("https://"):
        raise gl.vm.UserError("seal.policy_url must start with https://")
    if url != url.strip() or any(ch.isspace() for ch in url):
        raise gl.vm.UserError("seal.policy_url must not contain whitespace")
    rest = url[len("https://") :]
    if not rest:
        raise gl.vm.UserError("seal.policy_url must have a host")
    end = len(rest)
    for sep in ("/", "?", "#"):
        idx = rest.find(sep)
        if idx != -1 and idx < end:
            end = idx
    authority = rest[:end]
    if not authority:
        raise gl.vm.UserError("seal.policy_url must have a host")
    if "@" in authority:
        raise gl.vm.UserError("seal.policy_url must not contain credentials")
    return url


def _validate_tag(tag: typing.Any) -> str:
    """Validate a release tag into a conservative, URL-safe form."""
    if not isinstance(tag, str) or _TAG_RE.fullmatch(tag) is None:
        raise gl.vm.UserError(
            "tag must be 1-"
            + str(MAX_TAG_CHARS)
            + " characters of [A-Za-z0-9._-], starting alphanumeric"
        )
    return tag


def _tag_key(seal_id: str, tag: str) -> str:
    """Build the (seal, tag) index key.

    `seal_id` always matches `seal-<digits>` and validated tags never contain
    `/`, so this composite is unambiguous for every value that can be written.
    """
    return seal_id + "/" + tag


def _extract_release(payload: bytes) -> dict:
    """Extract ONLY the canonical release fields from a GitHub release document.

    Returns a dict with keys tag/author/published_at/assets/body_sha256. Raises
    ValueError if a required structured field is missing or malformed; the caller
    (`leader_fn`) converts any failure into release_ok=False.

    The free-form `body` is hashed and returned as a digest only. No prose is
    interpreted, so nothing a release author writes in the body can change tag,
    author, published_at, or assets.
    """
    doc = json.loads(payload.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("release payload is not a JSON object")

    tag_name = doc.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name:
        raise ValueError("release payload has no tag_name")

    author_obj = doc.get("author")
    if not isinstance(author_obj, dict):
        raise ValueError("release payload has no author object")
    author_login = author_obj.get("login")
    if not isinstance(author_login, str) or not author_login:
        raise ValueError("release payload has no author.login")

    published_raw = doc.get("published_at")
    if not isinstance(published_raw, str) or not published_raw:
        raise ValueError("release payload has no published_at")
    published_at = _parse_iso8601_to_unix(published_raw)

    asset_names = set()
    raw_assets = doc.get("assets")
    if isinstance(raw_assets, list):
        for item in raw_assets:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    asset_names.add(name)
    assets = sorted(asset_names)

    body_text = doc.get("body")
    if not isinstance(body_text, str):
        body_text = ""
    body_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    return {
        "tag": tag_name,
        "author": author_login,
        "published_at": published_at,
        "assets": assets,
        "body_sha256": body_sha256,
    }


def _parse_iso8601_to_unix(value: str) -> int:
    """Parse an ISO-8601 timestamp (GitHub's `published_at`) into unix seconds.

    Uses integer timedelta arithmetic on timezone-aware datetimes so conversion
    never depends on the host's local timezone or on floating point.
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    delta = parsed.astimezone(datetime.timezone.utc) - epoch
    return delta.days * 86400 + delta.seconds


def _is_plain_int(value: typing.Any) -> bool:
    """True for a JSON integer, and False for a bool (bool is an int subclass)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _as_bool(value: typing.Any) -> bool:
    return value if isinstance(value, bool) else False


def _as_int(value: typing.Any) -> int:
    return value if _is_plain_int(value) else 0


def _as_str(value: typing.Any) -> str:
    return value if isinstance(value, str) else ""


def _as_str_list(value: typing.Any) -> list:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_binding_map(value: typing.Any) -> dict:
    empty = {"tag": False, "author": False, "timing": False, "asset": False, "policy": False}
    if not isinstance(value, dict):
        return empty
    return {name: _as_bool(value.get(name)) for name in empty}
