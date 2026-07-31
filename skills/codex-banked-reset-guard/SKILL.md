---
name: codex-banked-reset-guard
description: Check Codex banked rate-limit reset credits through the local Codex app-server and safely redeem the earliest expiring available credit when it enters a configured expiry window. Use when the user asks about Codex reset-credit count or expiry, wants a dry run, explicitly wants to redeem an expiring banked reset, or wants an unattended reset-credit guard.
---

# Codex Banked Reset Guard

Use the bundled deterministic script. Delegate authentication and account selection to the local `codex app-server`. Never read or print `auth.json`.

Resolve the directory containing this `SKILL.md` as the Skill directory. On POSIX use `python3`; on Windows PowerShell use `py -3`.

## Check first

Default to a read-only status check.

POSIX:

```sh
python3 "$SKILL_DIR/scripts/codex_banked_reset_guard.py" status --json
```

Windows PowerShell, after assigning the Skill directory to `$SkillDir`:

```powershell
py -3 "$SkillDir\scripts\codex_banked_reset_guard.py" status --json
```

Return sanitized counts and expiry times in the user's language. Never expose raw credit IDs or raw app-server messages.

## Preview the one-hour guard

Run without mutation.

POSIX:

```sh
python3 "$SKILL_DIR/scripts/codex_banked_reset_guard.py" guard --within-hours 1 --json
```

Windows PowerShell:

```powershell
py -3 "$SkillDir\scripts\codex_banked_reset_guard.py" guard --within-hours 1 --json
```

## Apply only with explicit authorization

Only redeem when the user or an already-authorized scheduler explicitly requests actual redemption. Keep `--apply` visible.

POSIX:

```sh
python3 "$SKILL_DIR/scripts/codex_banked_reset_guard.py" guard --within-hours 1 --apply --json
```

Windows PowerShell:

```powershell
py -3 "$SkillDir\scripts\codex_banked_reset_guard.py" guard --within-hours 1 --apply --json
```

The script selects an exact available `creditId`, consumes at most one credit per logical attempt, and performs bounded read-after-write verification.

## Interpret the JSON exactly

- `checked`: read-only status completed.
- `not_due`: no credit is inside the one-hour window; no mutation occurred.
- `dry_run_due`: a credit is due, but `--apply` was absent.
- `reset`: the provider consumed the credit and the refreshed snapshot verified it.
- `alreadyRedeemed`: the same logical attempt had already succeeded and the refreshed snapshot verified it.
- `provider_confirmed_verification_pending`: the provider confirmed success (`applied: true`), but verification did not converge. Report the confirmed application and pending_attempt_preserved=true; the next authorized apply reuses the same logical attempt key.
- `deferred_nothing_to_reset`: no eligible quota window exists; no credit was consumed. A later scheduled invocation may try again.
- `no_credit`: no earned reset credit is available.
- `previous_attempt_reconciled_target_absent`: a previous uncertain target disappeared. Report `applied: null` and provider outcome unknown; do not claim it was consumed and do not claim it was not consumed.
- `already_running`: another apply guard holds the lock; this invocation sent no consume request.
- `consume_outcome_unknown`: a request may have reached the provider. Report `applied: null`; the script preserved the idempotent attempt for the next authorized apply.
- `interrupted_outcome_unknown`: a `guard --apply` run was interrupted. Report `applied: null` and provider outcome unknown; do not generate a new idempotency key outside the script.
- `provider_confirmed_state_cleanup_pending`: provider success is known (`applied: true`), but local cleanup failed. Report both facts and stop.
- `provider_outcome_state_cleanup_pending`: provider non-success is known (`applied: false`), but local cleanup failed.
- `previous_attempt_reconciled_state_cleanup_pending`: the prior target is absent, its provider outcome is unknown, and local cleanup failed.

Treat `reset_credits_unavailable`, `credit_details_unavailable`, `credit_details_incomplete`, `pending_state_invalid`, `pending_state_conflict`, `pending_target_not_actionable`, `guard_lock_unavailable`, `runtime_state_unavailable`, and `unsafe_codex_batch_path` as fail-closed errors. Lock-cleanup errors preserve any already-known provider and application facts. For any `ok: false` payload, report the exact sanitized `error` plus `applied` when present, then stop. Do not reinterpret a provider outcome as a script status.

## Safety rules

- Require `--apply` for every mutation-capable invocation.
- Refuse count-only, incomplete, malformed, expired, or unknown reset-credit details.
- Never call private `backend-api` or `wham` endpoints.
- Never ask the user to paste a token.
- Never print or persist full account IDs, credit IDs, access tokens, or raw protocol messages.
- Reuse only the script's sanitized pending-attempt state after an ambiguous timeout or an unverified provider-confirmed success; never invent a new retry key outside the script.
- Treat `nothingToReset` as deferred, not success.
- Do not claim verification when the refreshed snapshot did not converge.
- After a Codex upgrade, run status and dry-run checks before enabling unattended `--apply`; protocol-shape failures are a drift stop, not a reason to bypass the app-server.
