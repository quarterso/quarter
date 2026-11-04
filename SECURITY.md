# Security policy

Quarter's ledger handles vendor keys and records what agents spend. We take reports seriously.

## Reporting a vulnerability

The repository is private, so everyone who can read this can reach the maintainers directly. Send the report privately to a maintainer listed in `.github/CODEOWNERS`, by a direct message on GitHub or another private channel you already share. GitHub's private vulnerability reporting is not available on private repositories.

Do not open a public issue, pull request or discussion for a vulnerability.

Please include:

- what an attacker can do, and what they need first (an agent key, a person's key, network access);
- the steps or a request that shows it;
- the version or commit you tested.

Do not test against a gateway you do not run, and do not use real vendor keys or real money to show a problem. A stand-in vendor, like the ones in `tests/`, is enough.

## Supported versions

Quarter is pre-release. Only the `main` branch gets security fixes. Older commits and tags do not.

## What happens next

| Step | Target |
| --- | --- |
| We confirm we have your report | 3 working days |
| We tell you whether we accept it, and how severe we think it is | 10 working days |
| Fix for a critical or high severity issue on `main` | 30 days |
| Fix for a medium or low severity issue on `main` | 90 days |

We keep you told as we go. Once a fix is on `main`, we publish an advisory and credit you, unless you ask us not to.

## In scope

The gateway and dashboard (`proxy.py`, `dashboard.py`), egress rules (`rules.py`), run forensics (`forensics.py`), Slack approvals, the ledger and its hash chain, signed receipts and payment authorisations, the MCP server, the CLI and the Docker image.

Problems in a vendor's own API, in Slack, or in a deployment's reverse proxy are out of scope here. Report those to their owners.
