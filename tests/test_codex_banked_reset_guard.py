from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "codex-banked-reset-guard" / "scripts" / "codex_banked_reset_guard.py"
SPEC = importlib.util.spec_from_file_location("codex_banked_reset_guard", SCRIPT)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


NOW = 1_800_000_000.0
WINDOW_HOURS = guard.DEFAULT_WITHIN_HOURS
RAW_ID = "RateLimitResetCredit_super-secret-identifier"


def credit(expires_at: int, credit_id: str = RAW_ID, status: str = "available"):
    return guard.ResetCredit(
        credit_id=credit_id,
        status=status,
        reset_type="codexRateLimits",
        granted_at=int(NOW - 1000),
        expires_at=expires_at,
        title="Full reset",
    )


def snapshot(count, credits):
    value = None if credits is None else tuple(credits)
    return guard.ResetSnapshot(
        available_count=count,
        credits=value,
        rate_limits={"primary": {"usedPercent": 50}},
    )


class FakeClient:
    def __init__(self, reads, outcomes=None):
        self.reads = list(reads)
        self.outcomes = list(outcomes or [])
        self.consume_calls = []
        self.read_count = 0

    def read_rate_limits(self):
        self.read_count += 1
        if not self.reads:
            raise AssertionError("Unexpected read")
        if len(self.reads) == 1:
            value = self.reads[0]
        else:
            value = self.reads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def consume_reset(self, credit_id, idempotency_key):
        self.consume_calls.append((credit_id, idempotency_key))
        if not self.outcomes:
            raise AssertionError("Unexpected consume")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class DecisionTests(unittest.TestCase):
    def test_selects_earliest_credit_inside_one_hour_window(self):
        due_later = credit(int(NOW + 1 * 3600), "later")
        due_first = credit(int(NOW + 1800), "first")
        not_due = credit(int(NOW + 2 * 3600), "future")
        chosen = guard.select_due_credit(
            snapshot(3, [due_later, not_due, due_first]), NOW, WINDOW_HOURS
        )
        self.assertEqual(chosen.credit_id, "first")

    def test_exact_one_hour_boundary_is_due(self):
        chosen = guard.select_due_credit(
            snapshot(1, [credit(int(NOW + 1 * 3600))]), NOW, WINDOW_HOURS
        )
        self.assertIsNotNone(chosen)

    def test_expired_credit_is_not_due(self):
        chosen = guard.select_due_credit(
            snapshot(1, [credit(int(NOW - 1))]), NOW, WINDOW_HOURS
        )
        self.assertIsNone(chosen)

    def test_count_only_snapshot_fails_closed(self):
        client = FakeClient([snapshot(1, None)])
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "credit_details_unavailable")
        self.assertEqual(client.consume_calls, [])

    def test_zero_authoritative_count_with_null_details_is_safe_noop(self):
        client = FakeClient([snapshot(0, None)])

        payload = guard.run_guard(
            client, NOW, WINDOW_HOURS, apply=True
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "not_due")
        self.assertFalse(payload["applied"])
        self.assertEqual(
            payload["before"]["credit_detail_state"], "authoritative_empty"
        )
        self.assertEqual(client.consume_calls, [])

    def test_dry_run_never_consumes(self):
        client = FakeClient([snapshot(1, [credit(int(NOW + 3600))])])
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=False)
        self.assertEqual(payload["status"], "dry_run_due")
        self.assertFalse(payload["applied"])
        self.assertEqual(client.consume_calls, [])

    def test_reset_targets_exact_credit_and_verifies(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        client = FakeClient([before, after], ["reset"])
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
        self.assertEqual(payload["status"], "reset")
        self.assertTrue(payload["applied"])
        self.assertTrue(payload["verified"])
        self.assertEqual(client.consume_calls[0][0], RAW_ID)
        self.assertEqual(str(guard.uuid.UUID(client.consume_calls[0][1])), client.consume_calls[0][1])
        self.assertNotIn(RAW_ID, json.dumps(payload))

    def test_timeout_retry_reuses_idempotency_key(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        client = FakeClient(
            [before, after],
            [guard.RpcTimeout(), "alreadyRedeemed"],
        )
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
        self.assertEqual(payload["status"], "alreadyRedeemed")
        self.assertEqual(len(client.consume_calls), 2)
        self.assertEqual(client.consume_calls[0][1], client.consume_calls[1][1])

    def test_nothing_to_reset_is_deferred_not_success(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        client = FakeClient([before, before], ["nothingToReset"])
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
        self.assertEqual(payload["status"], "deferred_nothing_to_reset")
        self.assertFalse(payload["applied"])
        self.assertFalse(payload["verified"])
        self.assertEqual(client.read_count, 2)

    def test_capped_detail_absence_does_not_fake_verification(self):
        target = credit(int(NOW + 3600), "target")
        other = credit(int(NOW + 7200), "other")
        before = snapshot(2, [target])
        after = snapshot(2, [other])
        self.assertFalse(guard.target_absent_or_count_decreased(before, after, target))

    def test_truncated_credit_details_fail_closed_without_consuming(self):
        due = credit(int(NOW + 3600), "visible-but-not-global")
        client = FakeClient([snapshot(2, [due])], ["reset"])

        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "credit_details_incomplete")
        self.assertEqual(payload["error"], "credit_details_incomplete")
        self.assertEqual(client.consume_calls, [])

    def test_excess_or_duplicate_credit_details_fail_closed(self):
        first = credit(int(NOW + 3600), "duplicate")
        duplicate = credit(int(NOW + 7200), "duplicate")
        for invalid in (
            snapshot(1, [first, credit(int(NOW + 7200), "extra")]),
            snapshot(2, [first, duplicate]),
        ):
            with self.subTest(invalid=invalid):
                client = FakeClient([invalid], ["reset"])
                payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], "credit_details_incomplete")
                self.assertEqual(client.consume_calls, [])

    def test_missing_expiry_fails_closed(self):
        incomplete = credit(int(NOW + 3600))
        incomplete = guard.ResetCredit(
            credit_id=incomplete.credit_id,
            status=incomplete.status,
            reset_type=incomplete.reset_type,
            granted_at=incomplete.granted_at,
            expires_at=None,
            title=incomplete.title,
        )
        client = FakeClient([snapshot(1, [incomplete])], ["reset"])

        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "credit_details_incomplete")
        self.assertEqual(client.consume_calls, [])

    def test_missing_reset_credit_summary_is_a_guard_failure(self):
        client = FakeClient([snapshot(None, None)])
        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "reset_credits_unavailable")
        self.assertEqual(payload["error"], "reset_credits_unavailable")
        self.assertEqual(client.consume_calls, [])

    def test_count_drop_does_not_verify_while_target_remains(self):
        target = credit(int(NOW + 3600), "target")
        other = credit(int(NOW + 7200), "other")
        before = snapshot(2, [target, other])
        after = snapshot(1, [target])
        client = FakeClient([before, after], ["reset"])

        with mock.patch.object(guard.time, "sleep", return_value=None):
            payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertEqual(payload["status"], "provider_confirmed_verification_pending")
        self.assertEqual(payload["provider_outcome"], "reset")
        self.assertTrue(payload["applied"])
        self.assertFalse(payload["verified"])

    def test_provider_success_survives_all_verification_read_failures(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        client = FakeClient(
            [
                before,
                guard.RpcTimeout(),
                guard.RpcTransportError("opaque transport failure"),
                guard.RpcProtocolError("opaque protocol failure"),
            ],
            ["alreadyRedeemed"],
        )

        with mock.patch.object(guard.time, "sleep", return_value=None):
            payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "provider_confirmed_verification_pending")
        self.assertEqual(payload["provider_outcome"], "alreadyRedeemed")
        self.assertTrue(payload["applied"])
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["verification_attempts"], 3)
        self.assertEqual(payload["verification_error"], "app_server_protocol_error")

    def test_verification_loop_recovers_after_timeout_and_eof(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        client = FakeClient(
            [
                before,
                guard.RpcTimeout(),
                guard.RpcTransportError("EOF"),
                after,
            ],
            ["reset"],
        )

        with mock.patch.object(guard.time, "sleep", return_value=None):
            payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertEqual(payload["status"], "reset")
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["verification_attempts"], 3)
        self.assertNotIn("verification_error", payload)

    def test_final_consume_timeout_reconciles_and_reports_unknown(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        client = FakeClient(
            [before, after],
            [guard.RpcTimeout(), guard.RpcTimeout()],
        )

        payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "consume_outcome_unknown")
        self.assertEqual(payload["error"], "consume_outcome_unknown")
        self.assertIsNone(payload["applied"])
        self.assertEqual(payload["provider_outcome"], "unknown")
        self.assertTrue(payload["verified"])
        self.assertEqual(payload["consume_error"], "app_server_timeout")
        self.assertEqual(len(client.consume_calls), 2)
        self.assertEqual(client.consume_calls[0][1], client.consume_calls[1][1])

    def test_unknown_consume_render_error_remains_applied_unknown(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        client = FakeClient(
            [before, snapshot(0, [])],
            [guard.RpcTimeout(), guard.RpcTimeout()],
        )

        with mock.patch.object(
            guard,
            "add_verification_to_payload",
            side_effect=RuntimeError(RAW_ID),
        ):
            payload = guard.run_guard(
                client,
                NOW,
                WINDOW_HOURS,
                apply=True,
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "consume_outcome_unknown")
        self.assertEqual(payload["provider_outcome"], "unknown")
        self.assertIsNone(payload["applied"])
        self.assertEqual(
            payload["verification_error"],
            "verification_internal_error",
        )
        self.assertNotIn(RAW_ID, json.dumps(payload))

    def test_consume_protocol_error_is_treated_as_unknown_outcome(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        client = FakeClient(
            [before],
            [
                guard.RpcProtocolError("malformed response"),
                guard.RpcProtocolError("malformed response"),
            ],
        )

        with mock.patch.object(guard.time, "sleep", return_value=None):
            payload = guard.run_guard(
                client, NOW, WINDOW_HOURS, apply=True
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "consume_outcome_unknown")
        self.assertIsNone(payload["applied"])
        self.assertEqual(payload["consume_error"], "app_server_protocol_error")
        self.assertEqual(client.consume_calls[0][1], client.consume_calls[1][1])

    def test_final_transport_error_reconciles_even_when_reads_fail(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        client = FakeClient(
            [
                before,
                guard.RpcTimeout(),
                guard.RpcTimeout(),
                guard.RpcTransportError("EOF"),
            ],
            [
                guard.RpcTransportError("write uncertain"),
                guard.RpcTransportError("write uncertain"),
            ],
        )

        with mock.patch.object(guard.time, "sleep", return_value=None):
            payload = guard.run_guard(client, NOW, WINDOW_HOURS, apply=True)

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "consume_outcome_unknown")
        self.assertFalse(payload["verified"])
        self.assertEqual(payload["consume_error"], "app_server_transport_error")
        self.assertEqual(payload["verification_error"], "app_server_transport_error")

    def test_subsecond_remaining_display_matches_due_decision(self):
        expires_at = int(NOW + 1)
        item = credit(expires_at)
        before_expiry = expires_at - 0.25
        after_expiry = expires_at + 0.25

        due_summary = guard.credit_summary(item, before_expiry, WINDOW_HOURS)
        expired_summary = guard.credit_summary(item, after_expiry, WINDOW_HOURS)

        self.assertEqual(due_summary["remaining_seconds"], 1)
        self.assertTrue(due_summary["inside_guard_window"])
        self.assertIsNotNone(
            guard.select_due_credit(snapshot(1, [item]), before_expiry, WINDOW_HOURS)
        )
        self.assertEqual(expired_summary["remaining_seconds"], -1)
        self.assertFalse(expired_summary["inside_guard_window"])
        self.assertIsNone(
            guard.select_due_credit(snapshot(1, [item]), after_expiry, WINDOW_HOURS)
        )


class ParsingTests(unittest.TestCase):
    def test_parses_supported_app_server_shape(self):
        parsed = guard.parse_snapshot(
            {
                "rateLimits": {"primary": {"usedPercent": 10}},
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [
                        {
                            "id": RAW_ID,
                            "status": "available",
                            "resetType": "codexRateLimits",
                            "grantedAt": int(NOW),
                            "expiresAt": int(NOW + 3600),
                            "title": "Full reset",
                        }
                    ],
                },
            }
        )
        self.assertEqual(parsed.available_count, 1)
        self.assertEqual(parsed.credits[0].credit_id, RAW_ID)

    def test_rejects_invalid_credit_row(self):
        with self.assertRaises(guard.RpcProtocolError):
            guard.parse_snapshot(
                {
                    "rateLimits": {},
                    "rateLimitResetCredits": {
                        "availableCount": 1,
                        "credits": [{"status": "available"}],
                    },
                }
            )

    def test_rejects_provider_controlled_credit_enums_without_echoing_them(self):
        for field, value in (("status", RAW_ID), ("resetType", RAW_ID)):
            row = {
                "id": "opaque-id",
                "status": "available",
                "resetType": "codexRateLimits",
                "grantedAt": int(NOW),
                "expiresAt": int(NOW + 3600),
            }
            row[field] = value
            with self.subTest(field=field):
                with self.assertRaises(guard.RpcProtocolError) as raised:
                    guard.parse_snapshot(
                        {
                            "rateLimits": {},
                            "rateLimitResetCredits": {
                                "availableCount": 1,
                                "credits": [row],
                            },
                        }
                    )
                self.assertNotIn(RAW_ID, raised.exception.message)
                self.assertNotIn(
                    RAW_ID,
                    json.dumps(guard.error_payload("status", raised.exception)),
                )

    def test_rejects_non_utf8_credit_id_as_protocol_error(self):
        with self.assertRaises(guard.RpcProtocolError) as raised:
            guard.parse_snapshot(
                {
                    "rateLimits": {},
                    "rateLimitResetCredits": {
                        "availableCount": 1,
                        "credits": [
                            {
                                "id": "\ud800",
                                "status": "available",
                                "resetType": "codexRateLimits",
                                "grantedAt": int(NOW),
                                "expiresAt": int(NOW + 1800),
                            }
                        ],
                    },
                }
            )
        self.assertEqual(raised.exception.code, "app_server_protocol_error")
        json.dumps(guard.error_payload("status", raised.exception))

    def test_rejects_bool_for_integer_fields(self):
        valid_row = {
            "id": RAW_ID,
            "status": "available",
            "resetType": "codexRateLimits",
            "grantedAt": int(NOW),
            "expiresAt": int(NOW + 3600),
        }
        cases = [
            {
                "rateLimits": {},
                "rateLimitResetCredits": {
                    "availableCount": True,
                    "credits": [valid_row],
                },
            },
            {
                "rateLimits": {},
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [{**valid_row, "grantedAt": True}],
                },
            },
            {
                "rateLimits": {},
                "rateLimitResetCredits": {
                    "availableCount": 1,
                    "credits": [{**valid_row, "expiresAt": False}],
                },
            },
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(guard.RpcProtocolError):
                    guard.parse_snapshot(case)

    def test_rejects_unrepresentable_timestamps_as_protocol_errors(self):
        for field in ("grantedAt", "expiresAt"):
            row = {
                "id": RAW_ID,
                "status": "available",
                "resetType": "codexRateLimits",
                "grantedAt": int(NOW),
                "expiresAt": int(NOW + 3600),
            }
            row[field] = 10**100
            with self.subTest(field=field):
                with self.assertRaises(guard.RpcProtocolError) as raised:
                    guard.parse_snapshot(
                        {
                            "rateLimits": {},
                            "rateLimitResetCredits": {
                                "availableCount": 1,
                                "credits": [row],
                            },
                        }
                    )
                self.assertEqual(raised.exception.code, "app_server_protocol_error")

    def test_rejects_nan_and_infinity_cli_numbers(self):
        for option, value in (
            ("--within-hours", "nan"),
            ("--within-hours", "inf"),
            ("--within-hours", "-inf"),
            ("--timeout", "nan"),
            ("--timeout", "inf"),
        ):
            with self.subTest(option=option, value=value):
                with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
                    guard.parse_args(["guard", option, value])

    def test_strict_json_serialization_falls_back_to_safe_error(self):
        class ContextClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    guard, "resolve_app_server_command", return_value=["mock"]
                )
            )
            stack.enter_context(
                mock.patch.object(
                    guard, "AppServerClient", return_value=ContextClient()
                )
            )
            stack.enter_context(
                mock.patch.object(
                    guard,
                    "run_status",
                    return_value={
                        "ok": True,
                        "action": "status",
                        "value": float("nan"),
                    },
                )
            )
            printer = stack.enter_context(mock.patch("builtins.print"))
            exit_code = guard.main(["status", "--json"])

        self.assertEqual(exit_code, 1)
        output = printer.call_args.args[0]
        self.assertNotIn("NaN", output)
        parsed = json.loads(output)
        self.assertEqual(parsed["error"], "output_serialization_error")

    def test_serialization_error_preserves_provider_facts(self):
        result = {
            "ok": True,
            "action": "guard",
            "status": "reset",
            "provider_outcome": "reset",
            "apply_requested": True,
            "applied": True,
            "verified": True,
            "value": float("nan"),
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"CODEX_HOME": directory},
            clear=False,
        ), mock.patch.object(
            guard,
            "execute_action",
            return_value=result,
        ), mock.patch("builtins.print") as printer:
            exit_code = guard.main(["guard", "--apply", "--json"])

        self.assertEqual(exit_code, 1)
        output = printer.call_args.args[0]
        self.assertNotIn("NaN", output)
        parsed = json.loads(output)
        self.assertEqual(parsed["error"], "output_serialization_error")
        self.assertEqual(parsed["status"], "reset")
        self.assertEqual(parsed["provider_outcome"], "reset")
        self.assertTrue(parsed["applied"])
        self.assertTrue(parsed["verified"])


class RepositorySafetyTests(unittest.TestCase):
    def test_runtime_script_has_no_private_endpoint_or_auth_file_access(self):
        source = SCRIPT.read_text(encoding="utf-8").lower()
        for forbidden in ("backend-api", "/wham/", "auth.json", "authorization: bearer"):
            self.assertNotIn(forbidden, source)

    def test_skill_metadata_and_interface_are_present(self):
        skill_dir = ROOT / "skills" / "codex-banked-reset-guard"
        skill = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        interface = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: codex-banked-reset-guard\n"))
        self.assertIn("description:", skill.split("---", 2)[1])
        self.assertIn("$codex-banked-reset-guard", interface)



class RuntimeStateTests(unittest.TestCase):
    def make_store(self, directory):
        return guard.PendingAttemptStore(pathlib.Path(directory) / "pending-consume.json")

    def test_unknown_consume_reuses_persisted_key_on_next_invocation(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_client = FakeClient(
                [before],
                [guard.RpcTimeout(), guard.RpcTransportError("uncertain")],
            )
            with mock.patch.object(guard.time, "sleep", return_value=None):
                first = guard.run_guard(
                    first_client, NOW, WINDOW_HOURS, apply=True, pending_store=store
                )

            self.assertEqual(first["status"], "consume_outcome_unknown")
            self.assertTrue(first["pending_attempt_preserved"])
            persisted = store.load()
            self.assertIsNotNone(persisted)
            self.assertEqual(
                persisted.idempotency_key,
                first_client.consume_calls[0][1],
            )

            second_client = FakeClient([before, after], ["alreadyRedeemed"])
            second = guard.run_guard(
                second_client, NOW + 1, WINDOW_HOURS, apply=True, pending_store=store
            )

            self.assertEqual(second["status"], "alreadyRedeemed")
            self.assertTrue(second["resumed_pending_attempt"])
            self.assertEqual(
                second_client.consume_calls[0][1],
                first_client.consume_calls[0][1],
            )
            self.assertIsNone(store.load())

    def test_absent_pending_target_defers_any_new_redemption_for_one_run(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        absent = snapshot(0, [])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_client = FakeClient(
                [before, absent],
                [guard.RpcTimeout(), guard.RpcTimeout()],
            )
            first = guard.run_guard(
                first_client, NOW, WINDOW_HOURS, apply=True, pending_store=store
            )
            self.assertEqual(first["status"], "consume_outcome_unknown")
            self.assertTrue(first["verified"])

            second_client = FakeClient([absent])
            second = guard.run_guard(
                second_client, NOW + 1, WINDOW_HOURS, apply=True, pending_store=store
            )

            self.assertEqual(
                second["status"], "previous_attempt_reconciled_target_absent"
            )
            self.assertIsNone(second["applied"])
            self.assertTrue(second["verified"])
            self.assertEqual(second_client.consume_calls, [])
            self.assertIsNone(store.load())

    def test_known_provider_outcome_clears_pending_state(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        after = snapshot(0, [])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            client = FakeClient([before, after], ["reset"])

            payload = guard.run_guard(
                client, NOW, WINDOW_HOURS, apply=True, pending_store=store
            )

            self.assertEqual(payload["status"], "reset")
            self.assertTrue(payload["verified"])
            self.assertIsNone(store.load())

    def test_pending_file_never_contains_raw_credit_id(self):
        item = credit(int(NOW + 3600))
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.save(
                guard.PendingAttempt(
                    credit_sha256=item.digest,
                    idempotency_key=str(guard.uuid.uuid4()),
                    created_at=int(NOW),
                    expires_at=int(item.expires_at),
                )
            )
            encoded = store.path.read_text(encoding="utf-8")
            self.assertNotIn(RAW_ID, encoded)
            self.assertIn(item.digest, encoded)
            if os.name != "nt":
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_malformed_pending_state_fails_closed_without_disclosure(self):
        before = snapshot(1, [credit(int(NOW + 3600))])
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.path.write_text(
                json.dumps({"credit_sha256": RAW_ID}), encoding="utf-8"
            )
            client = FakeClient([before], ["reset"])

            with self.assertRaises(guard.GuardError) as raised:
                guard.run_guard(
                    client, NOW, WINDOW_HOURS, apply=True, pending_store=store
                )

            self.assertEqual(raised.exception.code, "pending_state_invalid")
            self.assertNotIn(RAW_ID, raised.exception.message)
            self.assertEqual(client.consume_calls, [])


    def test_deep_pending_json_is_stable_fail_closed_error(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.path.write_text("[" * 1100 + "0" + "]" * 1100, encoding="utf-8")
            with self.assertRaises(guard.GuardError) as raised:
                store.load()
            self.assertEqual(raised.exception.code, "pending_state_invalid")

    def test_interrupt_while_waiting_for_consume_is_unknown_and_persisted(self):
        before = snapshot(1, [credit(int(NOW + 1800))])

        class InterruptingClient(FakeClient):
            def consume_reset(self, credit_id, idempotency_key):
                self.consume_calls.append((credit_id, idempotency_key))
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            client = InterruptingClient([before])
            payload = guard.run_guard(
                client, NOW, WINDOW_HOURS, apply=True, pending_store=store
            )

            self.assertEqual(payload["status"], "consume_outcome_unknown")
            self.assertIsNone(payload["applied"])
            self.assertEqual(payload["consume_error"], "interrupted")
            self.assertTrue(payload["pending_attempt_preserved"])
            self.assertIsNotNone(store.load())

    def test_interrupt_after_provider_success_preserves_applied_fact(self):
        before = snapshot(1, [credit(int(NOW + 1800))])

        class VerificationInterruptClient(FakeClient):
            def read_rate_limits(self):
                self.read_count += 1
                if self.read_count == 1:
                    return before
                raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            client = VerificationInterruptClient([before], ["reset"])
            payload = guard.run_guard(
                client, NOW, WINDOW_HOURS, apply=True, pending_store=store
            )

            self.assertEqual(
                payload["status"], "provider_confirmed_verification_pending"
            )
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["verified"])
            self.assertEqual(payload["verification_error"], "verification_interrupted")
            self.assertTrue(payload["pending_attempt_preserved"])
            self.assertIsNotNone(store.load())

    def test_interrupt_during_verification_backoff_preserves_applied_fact(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        client = FakeClient([before, guard.RpcTimeout()], ["reset"])

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with mock.patch.object(guard.time, "sleep", side_effect=KeyboardInterrupt):
                payload = guard.run_guard(
                    client, NOW, WINDOW_HOURS, apply=True, pending_store=store
                )

            self.assertEqual(
                payload["status"], "provider_confirmed_verification_pending"
            )
            self.assertTrue(payload["applied"])
            self.assertFalse(payload["verified"])
            self.assertEqual(payload["verification_error"], "verification_interrupted")
            self.assertTrue(payload["pending_attempt_preserved"])
            self.assertIsNotNone(store.load())

    def test_unverified_provider_success_reuses_key_on_next_invocation(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        after = snapshot(0, [])

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_client = FakeClient([before], ["reset"])
            with mock.patch.object(guard.time, "sleep", return_value=None):
                first = guard.run_guard(
                    first_client,
                    NOW,
                    WINDOW_HOURS,
                    apply=True,
                    pending_store=store,
                )

            self.assertEqual(
                first["status"],
                "provider_confirmed_verification_pending",
            )
            self.assertTrue(first["applied"])
            self.assertTrue(first["pending_attempt_preserved"])
            first_key = first_client.consume_calls[0][1]
            self.assertIsNotNone(store.load())

            second_client = FakeClient([before, after], ["alreadyRedeemed"])
            second = guard.run_guard(
                second_client,
                NOW + 1,
                WINDOW_HOURS,
                apply=True,
                pending_store=store,
            )

            self.assertEqual(second["status"], "alreadyRedeemed")
            self.assertTrue(second["applied"])
            self.assertTrue(second["verified"])
            self.assertEqual(second_client.consume_calls[0][1], first_key)
            self.assertIsNone(store.load())

    def test_unexpected_state_cleanup_error_preserves_provider_fact(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        after = snapshot(0, [])

        for outcome, expected_applied, expected_status in (
            ("reset", True, "provider_confirmed_state_cleanup_pending"),
            ("nothingToReset", False, "provider_outcome_state_cleanup_pending"),
        ):
            with self.subTest(outcome=outcome):
                store = mock.Mock(spec=guard.PendingAttemptStore)
                store.load.return_value = None
                store.clear.side_effect = RuntimeError(RAW_ID)
                client = FakeClient(
                    [before, after if outcome == "reset" else before],
                    [outcome],
                )

                payload = guard.run_guard(
                    client,
                    NOW,
                    WINDOW_HOURS,
                    apply=True,
                    pending_store=store,
                )

                self.assertFalse(payload["ok"])
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(payload["provider_outcome"], outcome)
                self.assertIs(payload["applied"], expected_applied)
                self.assertEqual(
                    payload["error"],
                    "state_cleanup_internal_error",
                )
                self.assertNotIn(RAW_ID, json.dumps(payload))

    def test_unexpected_verification_render_error_preserves_success_fact(self):
        before = snapshot(1, [credit(int(NOW + 1800))])
        after = snapshot(0, [])
        client = FakeClient([before, after], ["reset"])

        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            with mock.patch.object(
                guard,
                "add_verification_to_payload",
                side_effect=RuntimeError(RAW_ID),
            ):
                payload = guard.run_guard(
                    client,
                    NOW,
                    WINDOW_HOURS,
                    apply=True,
                    pending_store=store,
                )

        self.assertEqual(
            payload["status"],
            "provider_confirmed_verification_pending",
        )
        self.assertEqual(payload["provider_outcome"], "reset")
        self.assertTrue(payload["applied"])
        self.assertFalse(payload["verified"])
        self.assertEqual(
            payload["verification_error"],
            "verification_internal_error",
        )
        self.assertNotIn(RAW_ID, json.dumps(payload))


class ProcessAndLockTests(unittest.TestCase):
    @staticmethod
    def pid_is_running(pid):
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            synchronize = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information | synchronize,
                False,
                pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    handle, ctypes.byref(exit_code)
                ):
                    return False
                return exit_code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        proc_stat = pathlib.Path("/proc") / str(pid) / "stat"
        try:
            if proc_stat.exists() and proc_stat.read_text().split()[2] == "Z":
                return False
        except (OSError, IndexError):
            pass
        return True

    def wait_until_gone(self, pids, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not any(self.pid_is_running(pid) for pid in pids):
                return True
            time.sleep(0.05)
        return not any(self.pid_is_running(pid) for pid in pids)

    def cleanup_owned_pids(self, pids):
        for pid in pids:
            if not self.pid_is_running(pid):
                continue
            if os.name == "nt":
                taskkill = pathlib.Path(
                    os.environ.get("SystemRoot", r"C:\Windows")
                ) / "System32" / "taskkill.exe"
                subprocess.run(
                    [str(taskkill), "/PID", str(pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_keyboard_interrupt_during_enter_closes_process(self):
        owned_pids = []
        client = guard.AppServerClient(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=1,
        )

        def interrupt_initialize(*_args, **_kwargs):
            self.assertIsNotNone(client.process)
            owned_pids.append(client.process.pid)
            raise KeyboardInterrupt()

        client.request = interrupt_initialize
        try:
            with self.assertRaises(KeyboardInterrupt):
                client.__enter__()
            self.assertTrue(self.wait_until_gone(owned_pids))
            self.assertIsNone(client.process)
            self.assertIsNone(client._stdout_thread)
            self.assertIsNone(client._stderr_thread)
        finally:
            self.cleanup_owned_pids(owned_pids)

    @unittest.skipUnless(os.name == "nt", "Windows Job startup interruption")
    def test_windows_job_assign_and_resume_interrupts_close_process(self):
        for hook_name in ("_assign_process_to_job", "_resume_suspended_process"):
            with self.subTest(hook=hook_name):
                owned_pids = []
                client = guard.AppServerClient(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=1,
                )

                def interrupt_startup(*_args, **_kwargs):
                    self.assertIsNotNone(client.process)
                    owned_pids.append(client.process.pid)
                    raise KeyboardInterrupt()

                try:
                    with mock.patch.object(
                        guard,
                        hook_name,
                        side_effect=interrupt_startup,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            client.__enter__()
                    self.assertTrue(self.wait_until_gone(owned_pids))
                    self.assertIsNone(client.process)
                    self.assertIsNone(client._job_handle)
                finally:
                    self.cleanup_owned_pids(owned_pids)

    @unittest.skipIf(os.name == "nt", "POSIX process-group interruption")
    def test_posix_cleanup_interrupt_still_kills_process_group(self):
        helper = ROOT / "tests" / "hanging_process_tree.py"
        owned_pids = []
        with tempfile.TemporaryDirectory() as directory:
            pid_file = pathlib.Path(directory) / "pids.json"
            process = subprocess.Popen(
                [sys.executable, str(helper), "parent", str(pid_file)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            client = guard.AppServerClient(["unused"], timeout=1)
            client.process = process
            original_wait = client._wait_without_raising
            wait_calls = 0

            def interrupt_first_wait(target, timeout):
                nonlocal wait_calls
                wait_calls += 1
                if wait_calls == 1:
                    raise KeyboardInterrupt()
                return original_wait(target, timeout)

            try:
                deadline = time.monotonic() + 3
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pid_file.exists())
                owned_pids = list(json.loads(pid_file.read_text()).values())

                with mock.patch.object(
                    client,
                    "_wait_without_raising",
                    side_effect=interrupt_first_wait,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        client._close_process()

                self.assertTrue(self.wait_until_gone(owned_pids))
                self.assertIsNone(client.process)
            finally:
                self.cleanup_owned_pids(owned_pids)
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        process.kill()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

    def test_hanging_process_tree_is_killed_within_bound(self):
        helper_source = ROOT / "tests" / "hanging_process_tree.py"
        owned_pids = []
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            helper = temp / "hanging_process_tree.py"
            shutil.copyfile(helper_source, helper)
            pid_file = temp / "pids.json"
            if os.name == "nt":
                shim = temp / "codex-hanging.cmd"
                shim.write_text(
                    '@echo off\r\n"{}" "{}" parent "{}"\r\n'.format(
                        sys.executable, helper, pid_file
                    ),
                    encoding="utf-8",
                )
                command = guard.resolve_app_server_command(str(shim))
            else:
                command = [sys.executable, str(helper), "parent", str(pid_file)]

            client = guard.AppServerClient(command, timeout=0.75)
            started = time.monotonic()
            try:
                with self.assertRaises(guard.RpcTimeout):
                    client.__enter__()
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 6.0)
                self.assertTrue(pid_file.exists())
                owned_pids = list(json.loads(pid_file.read_text()).values())
                self.assertTrue(self.wait_until_gone(owned_pids))
                self.assertIsNone(client.process)
                self.assertIsNone(client._stdout_thread)
                self.assertIsNone(client._stderr_thread)
            finally:
                self.cleanup_owned_pids(owned_pids)

    def test_descendant_is_killed_after_root_exits(self):
        helper_source = ROOT / "tests" / "hanging_process_tree.py"
        owned_pids = []
        with tempfile.TemporaryDirectory() as directory:
            temp = pathlib.Path(directory)
            helper = temp / "hanging_process_tree.py"
            shutil.copyfile(helper_source, helper)
            pid_file = temp / "pids.json"
            if os.name == "nt":
                shim = temp / "codex-exiting-root.cmd"
                shim.write_text(
                    '@echo off\r\n"{}" "{}" exiting-parent "{}"\r\n'.format(
                        sys.executable, helper, pid_file
                    ),
                    encoding="utf-8",
                )
                command = guard.resolve_app_server_command(str(shim))
            else:
                command = [
                    sys.executable,
                    str(helper),
                    "exiting-parent",
                    str(pid_file),
                ]

            client = guard.AppServerClient(command, timeout=0.75)
            started = time.monotonic()
            try:
                with self.assertRaises(guard.RpcTimeout):
                    client.__enter__()
                self.assertLess(time.monotonic() - started, 6.0)
                self.assertTrue(pid_file.exists())
                owned_pids = list(json.loads(pid_file.read_text()).values())
                self.assertTrue(self.wait_until_gone(owned_pids))
            finally:
                self.cleanup_owned_pids(owned_pids)

    def start_lock_holder(self, lock_path, ready_path):
        holder_code = r'''
import importlib.util
from pathlib import Path
import sys
script, lock_path, ready_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("guard_lock_holder", script)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
lock = module.NonBlockingApplyLock(Path(lock_path))
if not lock.acquire():
    raise SystemExit(3)
Path(ready_path).write_text("ready", encoding="utf-8")
sys.stdin.buffer.read(1)
lock.release()
'''
        return subprocess.Popen(
            [sys.executable, "-c", holder_code, str(SCRIPT), str(lock_path), str(ready_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def test_apply_lock_is_nonblocking_and_read_only_modes_bypass_it(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = pathlib.Path(directory) / guard.RUNTIME_DIRECTORY_NAME
            lock_path = state_dir / guard.LOCK_FILENAME
            ready_path = pathlib.Path(directory) / "ready"
            holder = self.start_lock_holder(lock_path, ready_path)
            try:
                deadline = time.monotonic() + 4
                while not ready_path.exists() and holder.poll() is None:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    ready_path.exists(), "lock holder did not become ready"
                )

                with mock.patch.dict(
                    os.environ, {"CODEX_HOME": directory}, clear=False
                ), mock.patch.object(
                    guard,
                    "execute_action",
                    return_value={"ok": True, "action": "status", "status": "checked"},
                ) as execute, mock.patch("builtins.print") as printer:
                    status_code = guard.main(["status", "--json"])
                    dry_code = guard.main(["guard", "--json"])
                    apply_code = guard.main(["guard", "--apply", "--json"])

                self.assertEqual(status_code, 0)
                self.assertEqual(dry_code, 0)
                self.assertEqual(apply_code, 1)
                self.assertEqual(execute.call_count, 2)
                busy = json.loads(printer.call_args.args[0])
                self.assertEqual(busy["status"], "already_running")
                self.assertEqual(busy["error"], "already_running")
                self.assertFalse(busy["applied"])
                self.assertNotIn(str(lock_path), json.dumps(busy))
            finally:
                if holder.stdin is not None:
                    try:
                        holder.stdin.write(b"x")
                        holder.stdin.close()
                    except OSError:
                        pass
                try:
                    holder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    holder.kill()
                    holder.wait(timeout=3)
                if holder.stderr is not None:
                    holder.stderr.close()

            recovered = guard.NonBlockingApplyLock(lock_path)
            self.assertTrue(recovered.acquire())
            recovered.release()

    @unittest.skipUnless(os.name == "nt", "Windows batch-path safety")
    def test_windows_batch_path_rejects_command_metacharacters(self):
        with tempfile.TemporaryDirectory() as directory:
            unsafe = pathlib.Path(directory) / "codex&unexpected.cmd"
            unsafe.write_text("@echo off\r\n", encoding="ascii")

            with self.assertRaises(guard.GuardError) as raised:
                guard.resolve_app_server_command(str(unsafe))

            self.assertEqual(raised.exception.code, "unsafe_codex_batch_path")
            self.assertNotIn(str(unsafe), raised.exception.message)

    @unittest.skipUnless(os.name == "nt", "Windows batch-path safety")
    def test_windows_batch_path_with_spaces_uses_safe_call_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            safe_dir = pathlib.Path(directory) / "safe space"
            safe_dir.mkdir()
            shim = safe_dir / "codex.cmd"
            shim.write_text("@echo off\r\n", encoding="ascii")

            command = guard.resolve_app_server_command(str(shim))

            self.assertEqual(command[4], "call")
            self.assertEqual(command[5], str(shim))
            self.assertEqual(command[-2:], ["app-server", "--stdio"])

    def test_bare_codex_ignores_cwd_and_relative_path_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            cwd = root / "workspace"
            trusted = root / "trusted"
            cwd.mkdir()
            trusted.mkdir()
            filename = "codex.cmd" if os.name == "nt" else "codex"
            (cwd / filename).write_text("malicious", encoding="utf-8")
            trusted_executable = trusted / filename
            trusted_executable.write_text("trusted", encoding="utf-8")
            if os.name != "nt":
                trusted_executable.chmod(0o700)

            previous_cwd = os.getcwd()
            try:
                os.chdir(str(cwd))
                with mock.patch.dict(
                    os.environ,
                    {"PATH": os.pathsep.join((str(cwd), "", ".", str(trusted)))},
                    clear=False,
                ):
                    command = guard.resolve_app_server_command("codex")
            finally:
                os.chdir(previous_cwd)

            resolved = command[5] if os.name == "nt" else command[0]
            self.assertEqual(resolved, str(trusted_executable))
            self.assertNotEqual(resolved, str(cwd / filename))

    def test_absolute_path_entry_below_cwd_is_still_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            trusted = root / "trusted"
            trusted.mkdir()
            filename = "codex.cmd" if os.name == "nt" else "codex"
            executable = trusted / filename
            executable.write_text("trusted", encoding="utf-8")
            if os.name != "nt":
                executable.chmod(0o700)
            with mock.patch.object(guard.os, "getcwd", return_value=str(root)), mock.patch.dict(
                os.environ, {"PATH": str(trusted)}, clear=False
            ):
                command = guard.resolve_app_server_command("codex")
            resolved = command[5] if os.name == "nt" else command[0]
            self.assertEqual(resolved, str(executable))

    def test_lock_release_interrupt_closes_handle(self):
        class FakeHandle:
            def __init__(self):
                self.closed = False

            def seek(self, *_args):
                return 0

            def fileno(self):
                return 123

            def close(self):
                self.closed = True

        handle = FakeHandle()
        lock = guard.NonBlockingApplyLock(pathlib.Path("unused"))
        lock._file = handle
        target = guard.msvcrt if os.name == "nt" else guard.fcntl
        function_name = "locking" if os.name == "nt" else "flock"

        with mock.patch.object(
            target,
            function_name,
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                lock.release()

        self.assertTrue(handle.closed)
        self.assertIsNone(lock._file)

    def test_cleanup_interrupt_preserves_already_computed_provider_result(self):
        class CleanupInterruptClient:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                raise KeyboardInterrupt()

        expected = {
            "ok": True,
            "action": "guard",
            "status": "reset",
            "provider_outcome": "reset",
            "applied": True,
            "verified": True,
        }
        args = guard.parse_args(["guard", "--apply", "--json"])
        with mock.patch.object(
            guard,
            "resolve_app_server_command",
            return_value=["mock"],
        ), mock.patch.object(
            guard,
            "AppServerClient",
            return_value=CleanupInterruptClient(),
        ), mock.patch.object(
            guard,
            "run_guard",
            return_value=dict(expected),
        ):
            payload = guard.execute_action(args, pending_store=mock.Mock())

        self.assertEqual(payload["status"], "reset")
        self.assertTrue(payload["applied"])
        self.assertTrue(payload["verified"])
        self.assertTrue(payload["process_cleanup_interrupted"])

    def test_main_lock_cleanup_failure_preserves_provider_result(self):
        expected = {
            "ok": True,
            "action": "guard",
            "status": "reset",
            "provider_outcome": "reset",
            "applied": True,
            "verified": True,
        }

        for failure, expected_error in (
            (KeyboardInterrupt(), "lock_cleanup_interrupted"),
            (RuntimeError(RAW_ID), "lock_cleanup_internal_error"),
        ):
            with self.subTest(error=expected_error):
                class FailingExitLock:
                    def __init__(self, _path):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        raise failure

                with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                    os.environ,
                    {"CODEX_HOME": directory},
                    clear=False,
                ), mock.patch.object(
                    guard,
                    "NonBlockingApplyLock",
                    FailingExitLock,
                ), mock.patch.object(
                    guard,
                    "execute_action",
                    return_value=dict(expected),
                ), mock.patch("builtins.print") as printer:
                    exit_code = guard.main(["guard", "--apply", "--json"])

                self.assertEqual(exit_code, 1)
                payload = json.loads(printer.call_args.args[0])
                self.assertEqual(payload["status"], "reset")
                self.assertEqual(payload["provider_outcome"], "reset")
                self.assertTrue(payload["applied"])
                self.assertTrue(payload["verified"])
                self.assertEqual(payload["error"], expected_error)
                self.assertNotIn(RAW_ID, json.dumps(payload))

    def test_top_level_apply_interrupt_is_never_reported_as_not_applied(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"CODEX_HOME": directory}, clear=False
        ), mock.patch.object(
            guard, "execute_action", side_effect=KeyboardInterrupt()
        ), mock.patch("builtins.print") as printer:
            exit_code = guard.main(["guard", "--apply", "--json"])

        self.assertEqual(exit_code, 1)
        payload = json.loads(printer.call_args.args[0])
        self.assertEqual(payload["status"], "interrupted_outcome_unknown")
        self.assertIsNone(payload["applied"])

    def test_unexpected_exception_is_sanitized(self):
        with mock.patch.object(
            guard, "execute_action", side_effect=RuntimeError(RAW_ID)
        ), mock.patch("builtins.print") as printer:
            exit_code = guard.main(["status", "--json"])

        self.assertEqual(exit_code, 1)
        output = printer.call_args.args[0]
        self.assertNotIn(RAW_ID, output)
        self.assertEqual(json.loads(output)["error"], "internal_error")


class HumanOutputTests(unittest.TestCase):
    def test_failure_output_preserves_confirmed_provider_facts(self):
        payload = {
            "ok": False,
            "status": "provider_confirmed_state_cleanup_pending",
            "error": "runtime_state_unavailable",
            "message": "cleanup failed",
            "provider_outcome": "reset",
            "applied": True,
            "verified": False,
        }
        rendered = "\n".join(guard.human_lines(payload))
        self.assertIn("Provider outcome: reset", rendered)
        self.assertIn("Applied: true", rendered)
        self.assertIn("Verified: false", rendered)
        self.assertIn("runtime_state_unavailable", rendered)

    def test_failure_output_marks_unknown_and_pending(self):
        payload = {
            "ok": False,
            "status": "consume_outcome_unknown",
            "error": "consume_outcome_unknown",
            "applied": None,
            "pending_attempt_preserved": True,
        }
        rendered = "\n".join(guard.human_lines(payload))
        self.assertIn("Applied: unknown", rendered)
        self.assertIn("Pending attempt preserved: true", rendered)


class TransportTests(unittest.TestCase):
    def test_stderr_drain_uses_raw_bytes_for_invalid_utf8(self):
        class StrictTextStream:
            def __init__(self):
                self.buffer = io.BytesIO(b"\xff" * 20000)

            @staticmethod
            def read(_size):
                raise AssertionError("text decoder must not be used")

        stream = StrictTextStream()
        guard.AppServerClient._drain_stderr(stream)
        self.assertEqual(stream.buffer.tell(), 20000)

    def test_stdout_message_flood_is_bounded_and_fails_closed(self):
        client = guard.AppServerClient(["unused"], timeout=1)
        notification = json.dumps({"method": "test/notification"}) + "\n"
        stream = io.StringIO(
            notification * (guard.MAX_STDOUT_QUEUE_MESSAGES + 1)
        )

        client._drain_stdout(stream)

        self.assertLessEqual(
            client._stdout_queue.qsize(), guard.MAX_STDOUT_QUEUE_MESSAGES
        )
        with self.assertRaisesRegex(
            guard.RpcProtocolError, "too many messages"
        ):
            client._wait_for_response(1)

    def test_writer_start_interrupt_closes_duplicate_fd(self):
        class FakeStdin:
            @staticmethod
            def fileno():
                return 55

        class FakeProcess:
            stdin = FakeStdin()

        client = guard.AppServerClient(["unused"], timeout=0.1)
        client.process = FakeProcess()
        with mock.patch.object(
            guard.os,
            "dup",
            return_value=123,
        ), mock.patch.object(
            guard.os,
            "close",
        ) as close_fd, mock.patch.object(
            guard.threading.Thread,
            "start",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                client._write({"id": 1})

        close_fd.assert_called_once_with(123)

    def test_blocked_stdin_write_is_bounded_and_aborts_process(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        client = guard.AppServerClient(["unused"], timeout=0.1)
        client.process = process
        started = time.monotonic()
        try:
            with self.assertRaises(guard.RpcTimeout):
                client._write({"blob": "x" * (2 * 1024 * 1024)})
            self.assertLess(time.monotonic() - started, 4.0)
            self.assertIsNotNone(process.poll())
        finally:
            client._close_process()

    def test_server_request_with_colliding_id_fails_without_write(self):
        client = guard.AppServerClient(["unused"], timeout=1)
        writes = []
        client._write = writes.append
        client._stdout_queue.put(
            (
                "line",
                json.dumps(
                    {
                        "id": 7,
                        "method": "server/needsClient",
                        "params": {"opaque": RAW_ID},
                    }
                ),
            )
        )

        with self.assertRaises(guard.RpcProtocolError):
            client._wait_for_response(7)

        self.assertEqual(writes, [])

    def test_response_id_requires_exact_integer_match(self):
        for bad_id in (True, 2, "1"):
            with self.subTest(bad_id=bad_id):
                client = guard.AppServerClient(["unused"], timeout=1)
                client._stdout_queue.put(
                    ("line", json.dumps({"id": bad_id, "result": {}}))
                )
                with self.assertRaises(guard.RpcProtocolError):
                    client._wait_for_response(1)

    def test_consume_rejects_non_string_outcome_as_protocol_error(self):
        for outcome in (None, [], {}, True, 1):
            with self.subTest(outcome=outcome):
                client = guard.AppServerClient(["unused"])
                client.request = lambda _method, _params, value=outcome: {
                    "outcome": value
                }
                with self.assertRaises(guard.RpcProtocolError):
                    client.consume_reset("opaque", "idempotency")

    def test_nonfinite_huge_and_deep_json_are_protocol_errors(self):
        invalid_lines = (
            '{"id":1,"result":{"value":NaN}}',
            '{"id":1,"result":{"value":1e999}}',
            '{"id":1,"result":{"value":' + "9" * 5000 + "}}",
            '{"id":1,"result":' + "[" * 1100 + "0" + "]" * 1100 + "}",
            " " * (guard.MAX_JSON_LINE_CHARS + 1),
        )
        for line in invalid_lines:
            with self.subTest(length=len(line)):
                client = guard.AppServerClient(["unused"], timeout=1)
                client._stdout_queue.put(("line", line))
                with self.assertRaises(guard.RpcProtocolError):
                    client._wait_for_response(1)

    def test_dynamic_json_rpc_error_code_is_not_disclosed(self):
        client = guard.AppServerClient(["unused"])
        client._write = lambda _message: None
        client._wait_for_response = lambda _request_id: {
            "id": 1,
            "error": {"code": RAW_ID, "message": RAW_ID},
        }

        with self.assertRaises(guard.RpcProtocolError) as raised:
            client.request("example")

        self.assertNotIn(RAW_ID, raised.exception.message)
        payload = guard.error_payload("status", raised.exception)
        self.assertNotIn(RAW_ID, json.dumps(payload))

    def test_exact_integer_json_rpc_error_code_may_be_disclosed(self):
        client = guard.AppServerClient(["unused"])
        client._write = lambda _message: None
        client._wait_for_response = lambda _request_id: {
            "id": 1,
            "error": {"code": -32001},
        }

        with self.assertRaises(guard.RpcProtocolError) as raised:
            client.request("example")

        self.assertEqual(
            raised.exception.message,
            "Codex app-server returned error -32001",
        )

    def test_invalid_utf8_credit_id_fails_before_consume(self):
        mock_server = ROOT / "tests" / "invalid_utf8_app_server.py"
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "consume-marker"
            with guard.AppServerClient(
                [sys.executable, str(mock_server), str(marker)],
                timeout=3,
            ) as client:
                with self.assertRaises(guard.RpcProtocolError) as raised:
                    guard.run_guard(
                        client,
                        NOW,
                        WINDOW_HOURS,
                        apply=True,
                    )

            self.assertEqual(
                raised.exception.code,
                "app_server_protocol_error",
            )
            self.assertFalse(marker.exists())

    def test_guard_applies_and_verifies_through_mock_app_server(self):
        mock_server = ROOT / "tests" / "mock_app_server.py"
        with guard.AppServerClient([sys.executable, str(mock_server)], timeout=3) as client:
            payload = guard.run_guard(client, guard.time.time(), WINDOW_HOURS, apply=True)
        self.assertEqual(payload["status"], "reset")
        self.assertTrue(payload["applied"])
        self.assertTrue(payload["verified"])

    def test_line_protocol_handshake_read_consume_and_refetch(self):
        mock_server = ROOT / "tests" / "mock_app_server.py"
        with guard.AppServerClient([sys.executable, str(mock_server)], timeout=3) as client:
            before = client.read_rate_limits()
            self.assertEqual(before.available_count, 1)
            outcome = client.consume_reset(before.credits[0].credit_id, str(guard.uuid.uuid4()))
            self.assertEqual(outcome, "reset")
            after = client.read_rate_limits()
            self.assertEqual(after.available_count, 0)


if __name__ == "__main__":
    unittest.main()
