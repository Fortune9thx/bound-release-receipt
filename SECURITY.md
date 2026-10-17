# Security

## Threat model

See [docs/DESIGN.md](docs/DESIGN.md#6-threat-model) for the full threat model: dishonest-leader fabrication, validator shape-checking, prompt injection via fetched content, silent required-fetch failure, policy-hash override, replay/double-attest, malformed caller input, unbounded URLs, and serialization drift — and exactly how each is closed or bounded.

## Reporting a vulnerability

If you find a security issue in this contract, please open a GitHub issue on this repository describing the problem. Do not include exploit code targeting live deployments in a public issue — describe the class of vulnerability and its impact, and a fix will be coordinated privately if needed.

## Scope

This contract has no admin keys, no upgradeability, and no privileged roles — `open_seal` and `attest` are callable by anyone, and every stored receipt is derived entirely from independently-verified consensus over live sources. There is no path by which a single party (including the contract deployer) can unilaterally write, alter, or fabricate stored receipt data, and a required-fetch failure can never produce an `attested` fold.
