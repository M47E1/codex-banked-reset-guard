# Codex Banked Reset Guard

A Codex Skill and dependency-free Python guard for banked Codex rate-limit reset credits. It uses the supported local `codex app-server` protocol to inspect expiry details, select an exact opaque credit ID, redeem at most one due credit, and verify the refreshed snapshot.

The default guard window is **1 hour before expiry**. Inspection and dry runs never mutate the account; redemption always requires `--apply`.

## What this repository is

This repository ships one installable Codex Skill plus its deterministic Python CLI, tests, and scheduler-ready automation contract. Codex uses the Skill instructions to choose and interpret commands; the bundled script performs the actual read or redemption. It is not a Codex plugin, MCP server, or hosted service.

## Safety boundary

The guard delegates authentication and account selection to `codex app-server`. It does not read `~/.codex/auth.json`, accept a backend URL, call private ChatGPT web endpoints, or print raw tokens, account IDs, credit IDs, or protocol responses.

It stops before mutation when the preflight response is malformed, omits exact credit rows, or returns fewer detail rows than the authoritative available count. It never reports an unknown or ambiguous consume outcome as success. A provider can also return `nothingToReset` when no current quota window is eligible; the guard does not manufacture usage to force eligibility.

## Requirements

Runtime:

- Python **3.9 through 3.14** (3.9 is the compatibility floor; prefer a currently maintained Python release)
- A current Codex CLI that provides `codex app-server`
- A signed-in ChatGPT/Codex account

Install or update the official Codex CLI with npm if needed:

```sh
npm install -g @openai/codex
codex --version
```

The [official Codex repository](https://github.com/openai/codex#installing-and-running-codex-cli) also documents platform installers and release binaries.

Skill installation only (not script runtime) uses the third-party [`skills` CLI](https://github.com/vercel-labs/skills). The commands below pin [`skills@1.5.21`](https://github.com/vercel-labs/skills/blob/v1.5.21/package.json); that release requires Node.js **22.20.0 or newer** and npm/npx. Future `skills` releases may change that minimum.

## Install the Skill for Codex

First verify that the repository exposes the expected Skill without installing it:

```sh
npx --yes skills@1.5.21 add https://github.com/M47E1/codex-banked-reset-guard --list
```

Then install only this Skill globally for Codex:

```sh
npx --yes skills@1.5.21 add https://github.com/M47E1/codex-banked-reset-guard --skill codex-banked-reset-guard -g -a codex
```

Verify the global Codex installation:

```sh
npx --yes skills@1.5.21 list -g -a codex
```

These commands target the public repository at `https://github.com/M47E1/codex-banked-reset-guard`.

The default Skill prompt is intentionally read-only:

```text
Use $codex-banked-reset-guard to inspect my banked Codex resets and report one-hour expiry risk without redeeming anything.
```

Actual redemption must be explicit:

```text
Use $codex-banked-reset-guard to apply the one-hour guard and actually redeem one due credit.
```

## Run directly

Run commands from the repository root.

### Windows PowerShell

Read-only status:

```powershell
py -3 .\skills\codex-banked-reset-guard\scripts\codex_banked_reset_guard.py status --json
```

Preview the one-hour decision:

```powershell
py -3 .\skills\codex-banked-reset-guard\scripts\codex_banked_reset_guard.py guard --within-hours 1 --json
```

Redeem at most one due credit:

```powershell
py -3 .\skills\codex-banked-reset-guard\scripts\codex_banked_reset_guard.py guard --within-hours 1 --apply --json
```

### POSIX shells

Read-only status:

```sh
python3 ./skills/codex-banked-reset-guard/scripts/codex_banked_reset_guard.py status --json
```

Preview the one-hour decision:

```sh
python3 ./skills/codex-banked-reset-guard/scripts/codex_banked_reset_guard.py guard --within-hours 1 --json
```

Redeem at most one due credit:

```sh
python3 ./skills/codex-banked-reset-guard/scripts/codex_banked_reset_guard.py guard --within-hours 1 --apply --json
```

Use `--human` instead of `--json` for compact terminal output.

## Result contract

| Script status | Meaning |
| --- | --- |
| `checked` | Read-only status completed. |
| `not_due` | No available credit expires inside the one-hour window. |
| `dry_run_due` | A credit is due, but `--apply` was not supplied. |
| `reset` | The provider consumed the selected credit and the refreshed snapshot verified it. |
| `alreadyRedeemed` | The same logical attempt had already succeeded and the refreshed snapshot verified it. |
| `provider_confirmed_verification_pending` | The provider confirmed success (`applied: true`), but bounded read-after-write verification did not converge. Pending state is retained so the same logical attempt reuses its idempotency key. |
| `deferred_nothing_to_reset` | No eligible quota window exists yet; no credit was consumed. |
| `no_credit` | No earned reset credit remains available. |
| `previous_attempt_reconciled_target_absent` | A previous uncertain target disappeared. No new consume was sent; the original provider outcome remains unknown (`applied: null`). |
| `already_running` | Another `guard --apply` holds the nonblocking lock. This invocation made no redemption attempt. |
| `consume_outcome_unknown` | A consume request may have reached the provider, but no outcome arrived. Sanitized pending state is preserved so the next apply can reuse the same idempotency key. |
| `interrupted_outcome_unknown` | A `guard --apply` run was interrupted. Report `applied: null` and provider outcome unknown; do not generate a new idempotency key outside the script. |
| `provider_confirmed_state_cleanup_pending` | The provider confirmed success (`applied: true`), but local pending-state cleanup failed. |
| `provider_outcome_state_cleanup_pending` | The provider returned a definite non-success outcome (`applied: false`), but local cleanup failed. |
| `previous_attempt_reconciled_state_cleanup_pending` | The prior target is absent and its provider result is unknown; local cleanup also failed. |

`reset_credits_unavailable`, `credit_details_unavailable`, `credit_details_incomplete`, `pending_state_invalid`, `pending_state_conflict`, `pending_target_not_actionable`, `guard_lock_unavailable`, `runtime_state_unavailable`, and `unsafe_codex_batch_path` are fail-closed errors. Lock-cleanup errors occur only after an operation result exists and preserve status, provider_outcome, applied, and verified. Other transport or protocol failures use sanitized `error` codes.

Interpret `applied` independently from `ok`:

- `applied: true` means the provider confirmed consumption even if `ok: false` reports a later cleanup failure.
- `applied: null` means the provider outcome is unknown; do not claim either success or non-consumption.
- `applied: false` means this invocation has no confirmed consumption.

For any `ok: false` payload, report the exact sanitized `error` and the `applied` value, then stop. Never fall back to a private endpoint, raw credential, unspecified-credit consume call, or manually generated retry key.

## Unattended guard

Run the apply command every 15–30 minutes with a trusted local scheduler. Each invocation exits when no credit is inside the one-hour window.

Configure the scheduler to:

- use the same OS user and `CODEX_HOME` as the signed-in Codex installation;
- prevent overlapping invocations;
- run missed invocations after sleep or reboot;
- capture only sanitized JSON;
- keep `--apply` visible in the scheduled command.

The guard enforces a nonblocking lock around `guard --apply`. Under `$CODEX_HOME/codex-banked-reset-guard` (default `~/.codex/codex-banked-reset-guard`) it keeps a one-byte lock file and an atomically written `pending-consume.json`. Pending state is retained while an outcome is uncertain or a provider-confirmed success has not yet verified. It contains a schema version, full SHA-256 credit digest, UUID idempotency key, and created/expiry timestamps—never the raw credit ID. The next authorized apply resumes that same logical attempt and reuses its key. Status and dry-run calls do not take the apply lock. No background loop or database is required.

Process I/O is bounded. Windows cleanup uses a kill-on-close Job Object; POSIX cleanup covers the app-server process group, including inherited children after the group leader exits. It does not claim to contain a child that intentionally creates a new POSIX session (setsid). Pending-state durability covers ordinary process crashes; sudden host or filesystem power loss is not an exactly-once guarantee on every platform.

## Protocol compatibility and drift gate

The implementation follows OpenAI's documented [`account/rateLimits/read`](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#7-rate-limits-chatgpt) and [`account/rateLimitResetCredit/consume`](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#8-earned-rate-limit-resets-chatgpt) contracts.

Compatibility was checked on **2026-07-31** against:

- the official Codex release [`rust-v0.146.0`](https://github.com/openai/codex/releases/tag/rust-v0.146.0);
- the locally installed `codex-cli 0.146.0-alpha.3.1`;
- a real, sanitized read-only `account/rateLimits/read` response.

Before enabling unattended `--apply` after a Codex update, run `status --json` and the guard command without `--apply`. Those strict reads are the live protocol-drift gate: incomplete detail lists, malformed shapes, unknown outcomes, and invalid timestamps stop before mutation.

## Test and release validation

The mock app-server suite never accesses a real account.

Windows PowerShell:

```powershell
py -3 -W error::ResourceWarning -m unittest discover -s tests -v
py -3 .\scripts\validate_release.py
```

POSIX:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 ./scripts/validate_release.py
```

GitHub Actions runs the suite on Ubuntu, Windows, and macOS with Python 3.9 and 3.14. The separate release gate discovers `skills/*/SKILL.md`, validates frontmatter/folder identity and UI metadata, enforces a read-only default prompt and the one-hour documentation contract, and checks the bundled script exists. Third-party Actions are pinned to full commit SHAs.

## License

MIT
