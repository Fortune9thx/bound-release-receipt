"""Live end-to-end smoke test for a deployed BoundReleaseReceipt contract.

This drives two consensus transactions against a real GitHub release:

  1. ``open_seal``  - seal a release channel (repo + policy digest + asset)
  2. ``attest``     - attest one release tag inside that seal

and then reads the resulting receipt back. It is the exact procedure used to
produce the live result recorded in ``docs/DESIGN.md`` section 9.

It never contains a key. Supply the signing key out of band:

    set BRR_PRIVATE_KEY=0x...            (PowerShell: $env:BRR_PRIVATE_KEY="0x...")
    python scripts/live_smoke.py

Optional environment:

    BRR_ADDRESS   deployed contract address (defaults to the reference deploy)
    BRR_NETWORK   studio-dev | bradbury | asimov | studionet  (default studio-dev)
    BRR_REPO      owner/name        (default Fortune9thx/bound-release-receipt)
    BRR_TAG       release tag       (default v1.0.0)
    BRR_POLICY_URL  https URL of the policy document
    BRR_REQUIRED_ASSET  required asset filename (default provenance.json)
    BRR_AUTHOR    allowed release author login (default Fortune9thx)

The script refuses to run without BRR_PRIVATE_KEY. It writes its full result to
``live_smoke_result.json`` next to the current working directory.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.request

from eth_account import Account
from genlayer_py import create_client
from genlayer_py.chains import (
    localnet,
    studio_devnet,
    studionet,
    testnet_asimov,
    testnet_bradbury,
)

DEFAULT_ADDRESS = "0xA85721785d6d3091458CbEAa1555149db322B9ca"

NETWORKS = {
    "studio-dev": studio_devnet,
    "studionet": studionet,
    "localnet": localnet,
    "asimov": testnet_asimov,
    "bradbury": testnet_bradbury,
}


def parse_iso8601_to_unix(value: str) -> int:
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    delta = parsed.astimezone(datetime.timezone.utc) - epoch
    return delta.days * 86400 + delta.seconds


def http_get(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req).read()


def main() -> int:
    parser = argparse.ArgumentParser(description="BoundReleaseReceipt live smoke test")
    parser.add_argument("--address", default=os.environ.get("BRR_ADDRESS", DEFAULT_ADDRESS))
    parser.add_argument("--network", default=os.environ.get("BRR_NETWORK", "studio-dev"))
    parser.add_argument(
        "--repo", default=os.environ.get("BRR_REPO", "Fortune9thx/bound-release-receipt")
    )
    parser.add_argument("--tag", default=os.environ.get("BRR_TAG", "v1.0.0"))
    parser.add_argument(
        "--policy-url",
        default=os.environ.get(
            "BRR_POLICY_URL",
            "https://raw.githubusercontent.com/Fortune9thx/bound-release-receipt/"
            "master/docs/POLICY.md",
        ),
    )
    parser.add_argument(
        "--required-asset",
        default=os.environ.get("BRR_REQUIRED_ASSET", "provenance.json"),
    )
    parser.add_argument("--author", default=os.environ.get("BRR_AUTHOR", "Fortune9thx"))
    args = parser.parse_args()

    private_key = os.environ.get("BRR_PRIVATE_KEY")
    if not private_key:
        print("BRR_PRIVATE_KEY is not set; refusing to run.", file=sys.stderr)
        return 2

    account = Account.from_key(private_key)
    client = create_client(chain=NETWORKS[args.network], account=account)
    print(f"signer:  {account.address}", flush=True)
    print(f"contract: {args.address}  network: {args.network}", flush=True)

    policy_sha256 = hashlib.sha256(
        http_get(args.policy_url, {"User-Agent": "bound-release-receipt"})
    ).hexdigest()
    release = json.loads(
        http_get(
            f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}",
            {
                "Accept": "application/vnd.github+json",
                "User-Agent": "bound-release-receipt",
            },
        )
    )
    published_at = parse_iso8601_to_unix(release["published_at"])

    seal = {
        "schema": "brr.seal.v1",
        "repo": args.repo,
        "policy_url": args.policy_url,
        "policy_sha256": policy_sha256,
        "allowed_authors": [args.author],
        "required_asset": args.required_asset,
        "window_start": published_at - 86400,
        "window_end": published_at + 30 * 86400,
    }
    seal_json = json.dumps(seal, sort_keys=True)
    print("seal: " + seal_json, flush=True)

    result = {"network": args.network, "address": args.address, "seal": seal}

    before = int(client.read_contract(args.address, "get_seal_count"))
    seal_fees = client.estimate_transaction_fees_for_write(
        args.address, "open_seal", args=[seal_json]
    )
    seal_tx = client.write_contract(
        args.address, "open_seal", args=[seal_json], fees=seal_fees
    )
    print(f"open_seal tx: {seal_tx}", flush=True)
    client.wait_for_transaction_receipt(
        seal_tx, wait_until="finalized", interval=5000, retries=240
    )
    seal_id = "seal-" + str(int(client.read_contract(args.address, "get_seal_count")) - 1)
    result["seal_tx"] = str(seal_tx)
    result["seal_id"] = seal_id
    print(f"seal_id: {seal_id}", flush=True)

    attest_fees = client.estimate_transaction_fees_for_write(
        args.address, "attest", args=[seal_id, args.tag]
    )
    attest_tx = client.write_contract(
        args.address, "attest", args=[seal_id, args.tag], fees=attest_fees
    )
    print(f"attest tx: {attest_tx}", flush=True)
    client.wait_for_transaction_receipt(
        attest_tx, wait_until="finalized", interval=5000, retries=240
    )
    result["attest_tx"] = str(attest_tx)

    receipt_id = str(client.read_contract(args.address, "get_latest_for_tag", args=[seal_id, args.tag]))
    receipt = client.read_contract(args.address, "get_receipt", args=[receipt_id])
    result["receipt_id"] = receipt_id
    result["receipt"] = json.loads(receipt) if isinstance(receipt, str) else receipt
    print(f"receipt_id: {receipt_id}", flush=True)
    print("receipt: " + json.dumps(result["receipt"], sort_keys=True), flush=True)
    print(f"fold: {result['receipt'].get('fold')}", flush=True)

    with open("live_smoke_result.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, sort_keys=True)

    return 0 if result["receipt"].get("fold") == "attested" else 1


if __name__ == "__main__":
    raise SystemExit(main())
