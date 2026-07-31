#!/usr/bin/env python3
"""Safely inspect and redeem expiring Codex banked reset credits.

The script speaks the documented line-delimited JSON protocol exposed by
codex app-server. Codex owns authentication; this script never reads tokens.
Output is sanitized and never contains raw reset-credit identifiers.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, IO, Iterable, List, Mapping, Optional, Sequence, Tuple

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt
else:
    import fcntl


VERSION = "0.2.0"
DEFAULT_WITHIN_HOURS = 1.0
SUCCESS_OUTCOMES = {"reset", "alreadyRedeemed"}
KNOWN_OUTCOMES = SUCCESS_OUTCOMES | {"nothingToReset", "noCredit"}
SAFE_FACT_STATUSES = {
    "reset",
    "alreadyRedeemed",
    "provider_confirmed_verification_pending",
    "deferred_nothing_to_reset",
    "no_credit",
    "previous_attempt_reconciled_target_absent",
    "consume_outcome_unknown",
    "interrupted_outcome_unknown",
    "provider_confirmed_state_cleanup_pending",
    "provider_outcome_state_cleanup_pending",
    "previous_attempt_reconciled_state_cleanup_pending",
}
GRACEFUL_CLOSE_SECONDS = 0.5
TREE_TERMINATE_SECONDS = 2.0
PROCESS_REAP_SECONDS = 1.0
DRAIN_JOIN_SECONDS = 0.5
RUNTIME_DIRECTORY_NAME = "codex-banked-reset-guard"
PENDING_STATE_FILENAME = "pending-consume.json"
LOCK_FILENAME = "guard-apply.lock"
MIN_I64 = -(2**63)
MAX_I64 = 2**63 - 1
MAX_JSON_LINE_CHARS = 1024 * 1024
MAX_STDOUT_QUEUE_MESSAGES = 64
MAX_JSON_NESTING = 64
MAX_JSON_INTEGER_DIGITS = 128
MAX_CREDIT_ID_BYTES = 4096


if os.name == "nt":
    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )


    class _IoCounters(ctypes.Structure):
        _fields_ = tuple(
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        )


    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )


    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        )


    def _windows_kernel32() -> Any:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
        )
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        )
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ThreadEntry32),
        )
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD
        return kernel32


    def _create_kill_on_close_job() -> int:
        kernel32 = _windows_kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Could not create process job")
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error_code = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error_code, "Could not configure process job")
        return int(handle)


    def _assign_process_to_job(job_handle: int, process_handle: int) -> None:
        kernel32 = _windows_kernel32()
        if not kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job_handle),
            wintypes.HANDLE(process_handle),
        ):
            raise OSError(ctypes.get_last_error(), "Could not assign process job")


    def _resume_suspended_process(process_id: int) -> None:
        kernel32 = _windows_kernel32()
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid_handle:
            raise OSError(ctypes.get_last_error(), "Could not enumerate process threads")
        resumed = False
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        try:
            has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
                if entry.th32OwnerProcessID == process_id:
                    thread_handle = kernel32.OpenThread(
                        0x0002,
                        False,
                        entry.th32ThreadID,
                    )
                    if thread_handle:
                        try:
                            result = kernel32.ResumeThread(thread_handle)
                            if result != 0xFFFFFFFF:
                                resumed = True
                        finally:
                            kernel32.CloseHandle(thread_handle)
                has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        if not resumed:
            raise OSError(ctypes.get_last_error(), "Could not resume process")


    def _terminate_and_close_job(job_handle: int) -> None:
        kernel32 = _windows_kernel32()
        try:
            kernel32.TerminateJobObject(wintypes.HANDLE(job_handle), 1)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(job_handle))


    def _close_job(job_handle: int) -> None:
        _windows_kernel32().CloseHandle(wintypes.HANDLE(job_handle))


class GuardError(Exception):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RpcTimeout(GuardError):
    def __init__(self, message: str = "Timed out waiting for Codex app-server") -> None:
        super().__init__("app_server_timeout", message)


class RpcTransportError(GuardError):
    def __init__(self, message: str) -> None:
        super().__init__("app_server_transport_error", message)


class RpcProtocolError(GuardError):
    def __init__(self, message: str) -> None:
        super().__init__("app_server_protocol_error", message)


@dataclass(frozen=True)
class ResetCredit:
    credit_id: str
    status: str
    reset_type: str
    granted_at: int
    expires_at: Optional[int]
    title: Optional[str]

    @property
    def fingerprint(self) -> str:
        return self.digest[:12]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.credit_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResetSnapshot:
    available_count: Optional[int]
    credits: Optional[Tuple[ResetCredit, ...]]
    rate_limits: Mapping[str, Any]


@dataclass(frozen=True)
class PendingAttempt:
    credit_sha256: str
    idempotency_key: str
    created_at: int
    expires_at: int


def runtime_directory() -> Path:
    """Return account-scoped runtime storage without reading Codex credentials."""

    configured = os.environ.get("CODEX_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".codex"
    if not base.is_absolute():
        base = Path.cwd() / base
    return base / RUNTIME_DIRECTORY_NAME


def ensure_runtime_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            path.chmod(0o700)
    except OSError as error:
        raise GuardError(
            "runtime_state_unavailable",
            "Could not prepare private runtime state; refusing to redeem.",
        ) from error


class NonBlockingApplyLock:
    """OS-released, account-scoped lock for the irreversible apply path."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Optional[IO[bytes]] = None

    def acquire(self) -> bool:
        if self._file is not None:
            raise GuardError(
                "guard_lock_unavailable",
                "The apply guard lock is already held by this instance.",
            )
        ensure_runtime_directory(self.path.parent)
        handle: Optional[IO[bytes]] = None
        try:
            handle = self.path.open("a+b")
            if os.name != "nt":
                os.chmod(str(self.path), 0o600)
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
        except OSError as error:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
            raise GuardError(
                "guard_lock_unavailable",
                "Could not open the apply guard lock; refusing to redeem.",
            ) from error
        assert handle is not None

        try:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            competing = error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
            if os.name == "nt" and getattr(error, "winerror", None) in (32, 33, 36):
                competing = True
            handle.close()
            if competing:
                return False
            raise GuardError(
                "guard_lock_unavailable",
                "Could not acquire the apply guard lock; refusing to redeem.",
            ) from error

        self._file = handle
        return True

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        cleanup_error: Optional[BaseException] = None
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        except BaseException as error:
            cleanup_error = error
        finally:
            try:
                handle.close()
            except OSError:
                pass
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None:
            raise cleanup_error

    def __enter__(self) -> "NonBlockingApplyLock":
        if not self.acquire():
            raise GuardError(
                "already_running",
                "Another apply guard run is already active; no redemption was attempted.",
            )
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.release()


def reject_nonfinite_json_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def parse_bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("oversized integer")
    return int(value)


def parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite number")
    return parsed


def json_nesting_within_limit(value: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return True
    return True


def json_strings_have_valid_utf8(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True
    if isinstance(value, Mapping):
        return all(
            json_strings_have_valid_utf8(key)
            and json_strings_have_valid_utf8(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(json_strings_have_valid_utf8(item) for item in value)
    return True

class PendingAttemptStore:
    """Atomic non-secret state used to reuse an uncertain consume request key."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Optional[PendingAttempt]:
        try:
            with self.path.open("rb") as handle:
                encoded = handle.read(4097)
            if len(encoded) > 4096:
                raise ValueError("oversized state")
            raw_text = encoded.decode("utf-8")
            if not json_nesting_within_limit(raw_text):
                raise ValueError("excessive nesting")
            raw = json.loads(
                raw_text,
                parse_constant=reject_nonfinite_json_constant,
                parse_int=parse_bounded_json_integer,
                parse_float=parse_finite_json_float,
            )
        except FileNotFoundError:
            return None
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            TypeError,
            RecursionError,
        ) as error:
            raise GuardError(
                "pending_state_invalid",
                "Pending consume state is unreadable or invalid; refusing to redeem.",
            ) from error

        if not isinstance(raw, Mapping):
            raise GuardError(
                "pending_state_invalid",
                "Pending consume state is unreadable or invalid; refusing to redeem.",
            )
        schema_version = raw.get("schema_version")
        credit_sha256 = raw.get("credit_sha256")
        idempotency_key = raw.get("idempotency_key")
        created_at = raw.get("created_at")
        expires_at = raw.get("expires_at")

        valid_digest = (
            isinstance(credit_sha256, str)
            and len(credit_sha256) == 64
            and all(character in "0123456789abcdef" for character in credit_sha256)
        )
        valid_key = False
        if isinstance(idempotency_key, str):
            try:
                valid_key = str(uuid.UUID(idempotency_key)) == idempotency_key
            except ValueError:
                valid_key = False
        if (
            type(schema_version) is not int
            or schema_version != 1
            or not valid_digest
            or not valid_key
            or type(created_at) is not int
            or created_at < 0
            or created_at > MAX_I64
            or type(expires_at) is not int
            or expires_at < 0
            or expires_at > MAX_I64
        ):
            raise GuardError(
                "pending_state_invalid",
                "Pending consume state is unreadable or invalid; refusing to redeem.",
            )
        try:
            utc_iso(created_at)
            utc_iso(expires_at)
        except RpcProtocolError as error:
            raise GuardError(
                "pending_state_invalid",
                "Pending consume state is unreadable or invalid; refusing to redeem.",
            ) from error
        return PendingAttempt(
            credit_sha256=credit_sha256,
            idempotency_key=idempotency_key,
            created_at=created_at,
            expires_at=expires_at,
        )

    def save(self, attempt: PendingAttempt) -> None:
        ensure_runtime_directory(self.path.parent)
        temporary_path: Optional[Path] = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=".pending-consume-",
                suffix=".tmp",
                dir=str(self.path.parent),
            )
            temporary_path = Path(temporary_name)
            if os.name != "nt":
                os.chmod(temporary_name, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": 1,
                        "credit_sha256": attempt.credit_sha256,
                        "idempotency_key": attempt.idempotency_key,
                        "created_at": attempt.created_at,
                        "expires_at": attempt.expires_at,
                    },
                    handle,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(self.path))
            temporary_path = None
            if os.name != "nt":
                os.chmod(str(self.path), 0o600)
        except (OSError, TypeError, ValueError) as error:
            raise GuardError(
                "runtime_state_unavailable",
                "Could not persist a safe consume attempt; no redemption was sent.",
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise GuardError(
                "runtime_state_unavailable",
                "Could not clear resolved consume state safely.",
            ) from error


@dataclass(frozen=True)
class VerificationResult:
    snapshot: Optional[ResetSnapshot]
    verified: bool
    attempts: int
    last_error: Optional[str]


def utc_iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    try:
        value = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise RpcProtocolError("A reset-credit timestamp is outside the supported range") from error
    return value.isoformat().replace("+00:00", "Z")


def local_iso(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    try:
        value = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError) as error:
        raise RpcProtocolError("A reset-credit timestamp is outside the supported range") from error
    return value.isoformat()


def parse_credit(raw: Mapping[str, Any]) -> ResetCredit:
    credit_id = raw.get("id")
    status = raw.get("status")
    reset_type = raw.get("resetType")
    granted_at = raw.get("grantedAt")
    expires_at = raw.get("expiresAt")

    if not isinstance(credit_id, str) or not credit_id:
        raise RpcProtocolError("A reset-credit detail row is missing a valid id")
    try:
        encoded_credit_id = credit_id.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RpcProtocolError(
            "A reset-credit detail row has an invalid id encoding"
        ) from error
    if len(encoded_credit_id) > MAX_CREDIT_ID_BYTES:
        raise RpcProtocolError("A reset-credit detail row has an oversized id")
    if status != "available":
        raise RpcProtocolError("A reset-credit detail row has an unsupported status")
    if reset_type != "codexRateLimits":
        raise RpcProtocolError("A reset-credit detail row has an unsupported resetType")
    if (
        type(granted_at) is not int
        or granted_at < MIN_I64
        or granted_at > MAX_I64
    ):
        raise RpcProtocolError("A reset-credit detail row is missing grantedAt")
    if expires_at is not None and (
        type(expires_at) is not int
        or expires_at < MIN_I64
        or expires_at > MAX_I64
    ):
        raise RpcProtocolError("A reset-credit detail row has an invalid expiresAt")

    # Validate both UTC and local rendering now so malformed backend data cannot
    # escape parsing and crash a later summary or write-result path.
    utc_iso(granted_at)
    local_iso(granted_at)
    if expires_at is not None:
        utc_iso(expires_at)
        local_iso(expires_at)

    title = raw.get("title")
    return ResetCredit(
        credit_id=credit_id,
        status=status,
        reset_type=reset_type,
        granted_at=granted_at,
        expires_at=expires_at,
        title=title if isinstance(title, str) else None,
    )


def parse_snapshot(result: Mapping[str, Any]) -> ResetSnapshot:
    if not isinstance(result, Mapping):
        raise RpcProtocolError("account/rateLimits/read returned a non-object result")

    summary = result.get("rateLimitResetCredits")
    rate_limits = result.get("rateLimits")
    if not isinstance(rate_limits, Mapping):
        raise RpcProtocolError("account/rateLimits/read did not include rateLimits")

    if summary is None:
        return ResetSnapshot(available_count=None, credits=None, rate_limits=rate_limits)
    if not isinstance(summary, Mapping):
        raise RpcProtocolError("rateLimitResetCredits has an invalid shape")

    count = summary.get("availableCount")
    if type(count) is not int or count < 0:
        raise RpcProtocolError("rateLimitResetCredits.availableCount is invalid")

    raw_credits = summary.get("credits")
    if raw_credits is None:
        credits: Optional[Tuple[ResetCredit, ...]] = None
    elif isinstance(raw_credits, list):
        credits = tuple(parse_credit(row) for row in raw_credits if isinstance(row, Mapping))
        if len(credits) != len(raw_credits):
            raise RpcProtocolError("rateLimitResetCredits.credits contains a non-object row")
    else:
        raise RpcProtocolError("rateLimitResetCredits.credits has an invalid shape")

    return ResetSnapshot(available_count=count, credits=credits, rate_limits=rate_limits)


def credit_details_error(snapshot: ResetSnapshot) -> Optional[str]:
    """Return a stable fail-closed reason when all available credits are not comparable."""

    if snapshot.available_count is None:
        return "reset_credits_unavailable"
    if snapshot.available_count == 0 and snapshot.credits is None:
        return None
    if snapshot.credits is None:
        return "credit_details_unavailable"
    if len(snapshot.credits) != snapshot.available_count:
        return "credit_details_incomplete"

    identifiers = {credit.credit_id for credit in snapshot.credits}
    if len(identifiers) != len(snapshot.credits):
        return "credit_details_incomplete"
    if any(
        credit.status != "available"
        or credit.reset_type != "codexRateLimits"
        or credit.expires_at is None
        for credit in snapshot.credits
    ):
        return "credit_details_incomplete"
    return None


def available_expiring_credits(snapshot: ResetSnapshot) -> List[ResetCredit]:
    if snapshot.credits is None:
        return []
    credits = [
        credit
        for credit in snapshot.credits
        if credit.status == "available"
        and credit.reset_type == "codexRateLimits"
        and credit.expires_at is not None
    ]
    return sorted(credits, key=lambda credit: (int(credit.expires_at or 0), credit.fingerprint))


def select_due_credit(
    snapshot: ResetSnapshot, now: float, within_hours: float
) -> Optional[ResetCredit]:
    threshold_seconds = within_hours * 60.0 * 60.0
    for credit in available_expiring_credits(snapshot):
        remaining = float(credit.expires_at or 0) - now
        if 0 < remaining <= threshold_seconds:
            return credit
    return None


def credit_summary(credit: ResetCredit, now: float, within_hours: float) -> Dict[str, Any]:
    remaining_seconds = None
    remaining_exact = None
    if credit.expires_at is not None:
        remaining_exact = float(credit.expires_at) - now
        if remaining_exact > 0:
            remaining_seconds = math.ceil(remaining_exact)
        elif remaining_exact < 0:
            remaining_seconds = math.floor(remaining_exact)
        else:
            remaining_seconds = 0
    return {
        "fingerprint": credit.fingerprint,
        "status": credit.status,
        "reset_type": credit.reset_type,
        "granted_at_utc": utc_iso(credit.granted_at),
        "expires_at_utc": utc_iso(credit.expires_at),
        "expires_at_local": local_iso(credit.expires_at),
        "remaining_seconds": remaining_seconds,
        "inside_guard_window": bool(
            remaining_exact is not None
            and remaining_exact > 0
            and remaining_exact <= within_hours * 3600
        ),
    }


def snapshot_summary(snapshot: ResetSnapshot, now: float, within_hours: float) -> Dict[str, Any]:
    if snapshot.available_count is None:
        detail_state = "unavailable"
    elif snapshot.available_count == 0 and snapshot.credits is None:
        detail_state = "authoritative_empty"
    elif snapshot.credits is None:
        detail_state = "count_only"
    elif credit_details_error(snapshot) is not None:
        detail_state = "incomplete"
    else:
        detail_state = "detailed"

    credits = [] if snapshot.credits is None else list(snapshot.credits)
    return {
        "available_count": snapshot.available_count,
        "credit_detail_state": detail_state,
        "detail_rows_returned": None if snapshot.credits is None else len(snapshot.credits),
        "credits": [credit_summary(item, now, within_hours) for item in credits],
    }


def target_absent_or_count_decreased(
    before: ResetSnapshot, after: ResetSnapshot, target: ResetCredit
) -> bool:
    # Keep the public helper name for compatibility. A lower total is not proof
    # that this exact opaque credit was consumed: a different credit may have
    # disappeared concurrently. Verification requires complete details and the
    # selected ID's absence.
    del before
    return credit_details_error(after) is None and all(
        credit.credit_id != target.credit_id for credit in after.credits or ()
    )


class AppServerClient:
    """Small sequential client for Codex's line-delimited JSON protocol."""

    def __init__(self, command: Sequence[str], timeout: float = 20.0) -> None:
        self.command = list(command)
        self.timeout = timeout
        self.process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue(
            maxsize=MAX_STDOUT_QUEUE_MESSAGES
        )
        self._next_id = 1
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._job_handle: Optional[int] = None

    def __enter__(self) -> "AppServerClient":
        creationflags = 0
        start_new_session = os.name != "nt"
        job_handle: Optional[int] = None
        try:
            if os.name == "nt":
                try:
                    job_handle = _create_kill_on_close_job()
                except OSError as error:
                    raise RpcTransportError(
                        "Could not create a safe Codex app-server process container"
                    ) from error
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
                )

            try:
                self.process = subprocess.Popen(
                    self.command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="strict",
                    bufsize=1,
                    creationflags=creationflags,
                    start_new_session=start_new_session,
                )
            except OSError as error:
                raise RpcTransportError("Could not start Codex app-server") from error

            # Transfer the local Job handle to cleanup ownership before any
            # assignment/resume operation that can be interrupted.
            self._job_handle = job_handle
            job_handle = None
            if self._job_handle is not None:
                try:
                    _assign_process_to_job(
                        self._job_handle,
                        int(self.process._handle),
                    )
                    _resume_suspended_process(self.process.pid)
                except OSError as error:
                    raise RpcTransportError(
                        "Could not contain and start Codex app-server safely"
                    ) from error

            assert self.process.stdout is not None
            assert self.process.stderr is not None
            self._stdout_thread = threading.Thread(
                target=self._drain_stdout,
                args=(self.process.stdout,),
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(self.process.stderr,),
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()
            self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-banked-reset-guard",
                        "title": "Codex Banked Reset Guard",
                        "version": VERSION,
                    },
                    "capabilities": {},
                },
            )
            self.notify("initialized")
        except BaseException:
            # __exit__ is not called when __enter__ fails. Transfer any still-local
            # Job handle, then close the entire startup boundary before re-raising.
            if self._job_handle is None and job_handle is not None:
                self._job_handle = job_handle
                job_handle = None
            try:
                self._close_process()
            except BaseException:
                # Preserve the original startup failure after best-effort hard cleanup.
                pass
            raise
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self._close_process()

    @staticmethod
    def _wait_without_raising(
        process: subprocess.Popen[str], timeout: float
    ) -> bool:
        try:
            process.wait(timeout=timeout)
            return True
        except (subprocess.TimeoutExpired, OSError):
            return False

    @staticmethod
    def _terminate_windows_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            system_root = os.environ.get("SystemRoot") or r"C:\Windows"
            taskkill = os.path.join(system_root, "System32", "taskkill.exe")
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=TREE_TERMINATE_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        AppServerClient._wait_without_raising(process, PROCESS_REAP_SECONDS)

    @staticmethod
    def _terminate_posix_group(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        AppServerClient._wait_without_raising(process, GRACEFUL_CLOSE_SECONDS)
        try:
            os.killpg(process.pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            group_exists = False
        else:
            group_exists = True
        if group_exists:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        AppServerClient._wait_without_raising(process, PROCESS_REAP_SECONDS)

    def _close_process(self) -> None:
        process = self.process
        job_handle = self._job_handle
        self.process = None
        self._job_handle = None
        stdout_thread = self._stdout_thread
        stderr_thread = self._stderr_thread
        interrupted: Optional[BaseException] = None
        tree_closed = False

        def remember_interrupt(error: BaseException) -> None:
            nonlocal interrupted
            if not isinstance(error, Exception) and interrupted is None:
                interrupted = error

        if process is None:
            if job_handle is not None and os.name == "nt":
                try:
                    _terminate_and_close_job(job_handle)
                except BaseException as error:
                    remember_interrupt(error)
            self._stdout_thread = None
            self._stderr_thread = None
            if interrupted is not None:
                raise interrupted
            return

        stdout = process.stdout
        stderr = process.stderr

        try:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except BaseException as error:
                    remember_interrupt(error)

            try:
                self._wait_without_raising(process, GRACEFUL_CLOSE_SECONDS)
            except BaseException as error:
                remember_interrupt(error)

            if os.name == "nt":
                # The Job covers descendants even when the root shim already exited.
                if job_handle is not None:
                    try:
                        _terminate_and_close_job(job_handle)
                        job_handle = None
                        tree_closed = True
                    except BaseException as error:
                        remember_interrupt(error)
                if process.poll() is None:
                    try:
                        self._terminate_windows_tree(process)
                        tree_closed = True
                    except BaseException as error:
                        remember_interrupt(error)
            else:
                # The process group can outlive its leader, so always close it.
                try:
                    self._terminate_posix_group(process)
                    tree_closed = True
                except BaseException as error:
                    remember_interrupt(error)
        finally:
            # A cleanup interrupt must not strand a tree after self.process has
            # already been cleared. Force the platform boundary before re-raising.
            if not tree_closed:
                if os.name == "nt":
                    if job_handle is not None:
                        try:
                            _terminate_and_close_job(job_handle)
                            job_handle = None
                            tree_closed = True
                        except BaseException as error:
                            remember_interrupt(error)
                    if process.poll() is None:
                        try:
                            self._terminate_windows_tree(process)
                            tree_closed = True
                        except BaseException as error:
                            remember_interrupt(error)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                        tree_closed = True
                    except (ProcessLookupError, PermissionError, OSError):
                        tree_closed = True
                    except BaseException as error:
                        remember_interrupt(error)
                    if process.poll() is None:
                        try:
                            process.kill()
                        except BaseException as error:
                            remember_interrupt(error)

            if job_handle is not None and os.name == "nt":
                try:
                    _terminate_and_close_job(job_handle)
                except BaseException as error:
                    remember_interrupt(error)

            try:
                process.wait(timeout=PROCESS_REAP_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass
            except BaseException as error:
                remember_interrupt(error)

            for thread in (stdout_thread, stderr_thread):
                if thread is not None:
                    try:
                        thread.join(timeout=DRAIN_JOIN_SECONDS)
                    except BaseException as error:
                        remember_interrupt(error)

            for stream, thread in (
                (stdout, stdout_thread),
                (stderr, stderr_thread),
            ):
                if stream is not None and (thread is None or not thread.is_alive()):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                    except BaseException as error:
                        remember_interrupt(error)

            self._stdout_thread = None
            self._stderr_thread = None

        if interrupted is not None:
            raise interrupted

    def _queue_stdout_event(self, kind: str, value: Any) -> bool:
        try:
            self._stdout_queue.put_nowait((kind, value))
            return True
        except queue.Full:
            try:
                while True:
                    self._stdout_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._stdout_queue.put_nowait(("overflow", None))
            except queue.Full:
                pass
            return False

    def _drain_stdout(self, stream: IO[str]) -> None:
        overflowed = False
        try:
            while True:
                line = stream.readline(MAX_JSON_LINE_CHARS + 1)
                if not line:
                    break
                if len(line) > MAX_JSON_LINE_CHARS:
                    if not self._queue_stdout_event("invalid", None):
                        overflowed = True
                        break
                    while line and not line.endswith("\n"):
                        line = stream.readline(MAX_JSON_LINE_CHARS + 1)
                    continue
                if line.strip() and not self._queue_stdout_event("line", line):
                    overflowed = True
                    break
        except UnicodeError:
            self._queue_stdout_event("invalid_encoding", None)
        except (OSError, ValueError):
            pass
        finally:
            if not overflowed:
                self._queue_stdout_event("eof", None)
    @staticmethod
    def _drain_stderr(stream: IO[str]) -> None:
        # stderr is discarded, so drain raw bytes and never let an invalid
        # diagnostic encoding stop the pipe consumer.
        raw_stream = getattr(stream, "buffer", stream)
        try:
            while raw_stream.read(8192):
                pass
        except (OSError, UnicodeError, ValueError):
            pass

    def _abort_blocked_io(self) -> None:
        """Hard-stop the process boundary without touching a possibly locked stdin."""

        process = self.process
        if process is None:
            return
        if os.name == "nt":
            job_handle = self._job_handle
            self._job_handle = None
            if job_handle is not None:
                try:
                    _terminate_and_close_job(job_handle)
                except BaseException:
                    pass
            if process.poll() is None:
                try:
                    self._terminate_windows_tree(process)
                except BaseException:
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            except BaseException:
                pass
            if process.poll() is None:
                try:
                    process.kill()
                except BaseException:
                    pass
        try:
            process.wait(timeout=PROCESS_REAP_SECONDS)
        except BaseException:
            pass

    def _write(self, message: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise RpcTransportError("Codex app-server is not running")
        try:
            encoded = (
                json.dumps(
                    message,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise RpcProtocolError(
                "The app-server request could not be encoded safely"
            ) from error

        try:
            write_fd = os.dup(process.stdin.fileno())
        except (OSError, ValueError) as error:
            raise RpcTransportError(
                "Codex app-server input closed unexpectedly"
            ) from error

        result: "queue.Queue[Optional[BaseException]]" = queue.Queue()

        def write_all() -> None:
            error: Optional[BaseException] = None
            offset = 0
            try:
                while offset < len(encoded):
                    written = os.write(write_fd, encoded[offset:])
                    if written <= 0:
                        raise OSError("short pipe write")
                    offset += written
            except BaseException as caught:
                error = caught
            finally:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
                result.put(error)

        writer = threading.Thread(target=write_all, daemon=True)
        try:
            writer.start()
        except BaseException as error:
            try:
                os.close(write_fd)
            except OSError:
                pass
            if isinstance(error, Exception):
                raise RpcTransportError(
                    "Could not start the bounded app-server writer"
                ) from error
            raise

        try:
            writer.join(timeout=self.timeout)
        except BaseException:
            self._abort_blocked_io()
            try:
                writer.join(timeout=DRAIN_JOIN_SECONDS)
            except BaseException:
                pass
            raise

        if writer.is_alive():
            self._abort_blocked_io()
            try:
                writer.join(timeout=DRAIN_JOIN_SECONDS)
            except BaseException:
                pass
            raise RpcTimeout("Timed out writing to Codex app-server")

        try:
            write_error = result.get_nowait()
        except queue.Empty as error:
            raise RpcTransportError(
                "Codex app-server writer ended without a result"
            ) from error
        if write_error is not None:
            raise RpcTransportError(
                "Codex app-server input closed unexpectedly"
            ) from write_error
    def notify(self, method: str, params: Any = None) -> None:
        message: Dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(self, method: str, params: Any = None) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None or method == "account/rateLimits/read":
            message["params"] = params
        self._write(message)
        response = self._wait_for_response(request_id)

        if "error" in response:
            error = response.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                # JSON-RPC codes are integers. Never echo provider-controlled
                # strings or int-like subclasses into sanitized output.
                public_code = " " + str(code) if type(code) is int else ""
                raise RpcProtocolError("Codex app-server returned error" + public_code)
            raise RpcProtocolError("Codex app-server returned an error")

        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RpcProtocolError("Codex app-server returned an invalid result")
        return result

    def _wait_for_response(self, request_id: Any) -> Mapping[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RpcTimeout()
            try:
                kind, value = self._stdout_queue.get(timeout=remaining)
            except queue.Empty as error:
                raise RpcTimeout() from error

            if kind == "eof":
                raise RpcTransportError("Codex app-server exited unexpectedly")
            if kind == "invalid_encoding":
                raise RpcProtocolError("Codex app-server emitted invalid UTF-8")
            if kind == "overflow":
                raise RpcProtocolError("Codex app-server emitted too many messages")
            if kind == "invalid" or value is None:
                raise RpcProtocolError("Codex app-server emitted an oversized JSON message")
            if len(value) > MAX_JSON_LINE_CHARS:
                raise RpcProtocolError("Codex app-server emitted an oversized JSON message")
            if not json_nesting_within_limit(value):
                raise RpcProtocolError("Codex app-server emitted excessively nested JSON")

            try:
                message = json.loads(
                    value,
                    parse_constant=reject_nonfinite_json_constant,
                    parse_int=parse_bounded_json_integer,
                    parse_float=parse_finite_json_float,
                )
            except (ValueError, RecursionError) as error:
                raise RpcProtocolError("Codex app-server emitted invalid JSON") from error
            if not isinstance(message, Mapping):
                raise RpcProtocolError("Codex app-server emitted a non-object message")
            if not json_strings_have_valid_utf8(message):
                raise RpcProtocolError(
                    "Codex app-server emitted invalid Unicode text"
                )

            response_id = message.get("id")
            if "method" in message:
                method = message.get("method")
                if not isinstance(method, str):
                    raise RpcProtocolError("Codex app-server emitted a request with an invalid method")
                if response_id is not None:
                    if type(response_id) is not int and not isinstance(
                        response_id, str
                    ):
                        raise RpcProtocolError(
                            "Codex app-server emitted a request with an invalid id"
                        )
                    # This guard has no server-request capability. Fail closed
                    # without writing a response: a peer that is not reading
                    # stdin must not bypass the RPC deadline via pipe backpressure.
                    raise RpcProtocolError(
                        "Codex app-server emitted an unsupported server request"
                    )
                continue
            if response_id is None:
                continue
            if type(response_id) is not int:
                raise RpcProtocolError("Codex app-server emitted an invalid response id")
            if "result" not in message and "error" not in message:
                raise RpcProtocolError("Codex app-server emitted an invalid response")
            if response_id != request_id:
                raise RpcProtocolError("Codex app-server emitted an unexpected response id")
            return message

    def read_rate_limits(self) -> ResetSnapshot:
        return parse_snapshot(self.request("account/rateLimits/read", None))

    def consume_reset(self, credit_id: str, idempotency_key: str) -> str:
        result = self.request(
            "account/rateLimitResetCredit/consume",
            {"idempotencyKey": idempotency_key, "creditId": credit_id},
        )
        outcome = result.get("outcome")
        if not isinstance(outcome, str) or outcome not in KNOWN_OUTCOMES:
            raise RpcProtocolError("The reset consume response has an unknown outcome")
        return outcome


def paths_equal(first: str, second: str) -> bool:
    try:
        first_real = os.path.normcase(os.path.realpath(first))
        second_real = os.path.normcase(os.path.realpath(second))
        return first_real == second_real
    except (OSError, ValueError):
        return False


def safe_path_lookup(executable: str) -> Optional[str]:
    """Resolve a bare command without consulting cwd or relative PATH entries."""

    if os.path.dirname(executable) or os.path.isabs(executable):
        return None
    current_directory = os.getcwd()
    if os.name == "nt" and not os.path.splitext(executable)[1]:
        allowed_extensions = {".com", ".exe", ".bat", ".cmd"}
        configured = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        extensions = []
        for extension in configured.split(os.pathsep):
            normalized = extension.strip().lower()
            if normalized in allowed_extensions and normalized not in extensions:
                extensions.append(normalized)
        if not extensions:
            extensions = [".com", ".exe", ".bat", ".cmd"]
    else:
        extensions = [""]

    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        cleaned = raw_directory.strip().strip('"')
        if not cleaned:
            continue
        expanded = os.path.expandvars(os.path.expanduser(cleaned))
        if not os.path.isabs(expanded):
            continue
        directory = os.path.abspath(expanded)
        if paths_equal(directory, current_directory):
            continue
        for extension in extensions:
            candidate = os.path.abspath(os.path.join(directory, executable + extension))
            if not os.path.isfile(candidate):
                continue
            if os.name != "nt" and not os.access(candidate, os.X_OK):
                continue
            return candidate
    return None


def resolve_app_server_command(codex_bin: str) -> List[str]:
    has_explicit_path = bool(os.path.dirname(codex_bin) or os.path.isabs(codex_bin))
    if has_explicit_path:
        candidate = os.path.abspath(os.path.expanduser(codex_bin))
        resolved = candidate if os.path.isfile(candidate) else None
    else:
        resolved = safe_path_lookup(codex_bin)
    if resolved is None:
        raise GuardError("codex_not_found", "Could not find the Codex CLI executable")

    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        if any(character in resolved for character in '&|<>^()%!"\r\n'):
            raise GuardError(
                "unsafe_codex_batch_path",
                "The resolved Codex batch path contains unsafe command characters.",
            )
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        command_shell = os.path.join(system_root, "System32", "cmd.exe")
        return [
            command_shell,
            "/d",
            "/s",
            "/c",
            "call",
            resolved,
            "app-server",
            "--stdio",
        ]
    return [resolved, "app-server", "--stdio"]


def base_payload(action: str, now: float, within_hours: float) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "tool_version": VERSION,
        "ok": True,
        "action": action,
        "checked_at_utc": utc_iso(now),
        "guard_window_hours": within_hours,
    }


def run_status(client: Any, now: float, within_hours: float) -> Dict[str, Any]:
    snapshot = client.read_rate_limits()
    payload = base_payload("status", now, within_hours)
    payload["status"] = "checked"
    payload.update(snapshot_summary(snapshot, now, within_hours))
    return payload


def read_verification_snapshot(
    client: Any,
    before: ResetSnapshot,
    target: ResetCredit,
    attempts: int = 3,
    delay_seconds: float = 0.25,
) -> VerificationResult:
    """Refetch a bounded number of times without erasing a confirmed write."""

    latest: Optional[ResetSnapshot] = None
    last_error: Optional[str] = None
    completed_attempts = 0
    for attempt in range(attempts):
        try:
            if attempt and delay_seconds > 0:
                time.sleep(delay_seconds)
            completed_attempts += 1
            candidate = client.read_rate_limits()
            target_absent = target_absent_or_count_decreased(
                before, candidate, target
            )
        except KeyboardInterrupt:
            return VerificationResult(
                snapshot=latest,
                verified=False,
                attempts=completed_attempts,
                last_error="verification_interrupted",
            )
        except GuardError as error:
            last_error = error.code
            continue
        except Exception:
            last_error = "verification_internal_error"
            continue

        latest = candidate
        last_error = None
        if target_absent:
            return VerificationResult(
                snapshot=candidate,
                verified=True,
                attempts=completed_attempts,
                last_error=None,
            )
    return VerificationResult(
        snapshot=latest,
        verified=False,
        attempts=completed_attempts,
        last_error=last_error,
    )


def add_verification_to_payload(
    payload: Dict[str, Any],
    verification: VerificationResult,
    now: float,
    within_hours: float,
) -> None:
    if verification.snapshot is not None:
        payload["after"] = snapshot_summary(verification.snapshot, now, within_hours)
    payload["verified"] = verification.verified
    payload["verification_attempts"] = verification.attempts
    if verification.last_error is not None:
        payload["verification_error"] = verification.last_error


def run_guard(
    client: Any,
    now: float,
    within_hours: float,
    apply: bool,
    pending_store: Optional[PendingAttemptStore] = None,
) -> Dict[str, Any]:
    before = client.read_rate_limits()
    payload = base_payload("guard", now, within_hours)
    payload["apply_requested"] = apply
    payload["before"] = snapshot_summary(before, now, within_hours)

    details_error = credit_details_error(before)
    if details_error is not None:
        if details_error == "reset_credits_unavailable":
            message = "The backend did not provide reset-credit availability."
        elif details_error == "credit_details_unavailable":
            message = "Only the count is available; refusing to redeem without exact details."
        else:
            message = (
                "Reset-credit details are incomplete or contradict the authoritative count; "
                "refusing to redeem."
            )
        payload.update(
            ok=False,
            status=details_error,
            applied=False,
            error=details_error,
            message=message,
        )
        return payload

    pending = pending_store.load() if apply and pending_store is not None else None
    target: Optional[ResetCredit] = None
    attempt_key: Optional[str] = None
    if pending is not None:
        target = next(
            (
                credit
                for credit in before.credits or ()
                if credit.digest == pending.credit_sha256
            ),
            None,
        )
        if target is None:
            try:
                pending_store.clear()
            except GuardError as error:
                payload.update(
                    ok=False,
                    status="previous_attempt_reconciled_state_cleanup_pending",
                    applied=None,
                    verified=True,
                    provider_outcome="unknown",
                    error=error.code,
                    state_error=error.code,
                    message=(
                        "The previous target is absent, but its local reconciliation "
                        "state could not be cleared."
                    ),
                )
                return payload
            payload.update(
                status="previous_attempt_reconciled_target_absent",
                applied=None,
                verified=True,
                provider_outcome="unknown",
                message=(
                    "The previous uncertain target is no longer available; no new "
                    "redemption was attempted in this run."
                ),
            )
            return payload
        if target.expires_at != pending.expires_at:
            payload.update(
                ok=False,
                status="pending_state_conflict",
                applied=False,
                error="pending_state_conflict",
                message=(
                    "Pending consume state does not match the current credit details; "
                    "refusing to redeem."
                ),
            )
            return payload
        if target.expires_at is None or target.expires_at <= now:
            payload.update(
                ok=False,
                status="pending_target_not_actionable",
                applied=False,
                error="pending_target_not_actionable",
                message=(
                    "The pending consume target is no longer actionable; refusing to "
                    "redeem another credit."
                ),
            )
            return payload
        attempt_key = pending.idempotency_key
        payload["resumed_pending_attempt"] = True
    else:
        target = select_due_credit(before, now, within_hours)

    if target is None:
        payload.update(status="not_due", applied=False)
        return payload

    payload["selected_credit"] = credit_summary(target, now, within_hours)
    if not apply:
        payload.update(status="dry_run_due", applied=False)
        return payload

    if attempt_key is None:
        attempt_key = str(uuid.uuid4())
        if pending_store is not None:
            assert target.expires_at is not None
            pending_store.save(
                PendingAttempt(
                    credit_sha256=target.digest,
                    idempotency_key=attempt_key,
                    created_at=int(now),
                    expires_at=target.expires_at,
                )
            )

    outcome: Optional[str] = None
    uncertain_error: Optional[GuardError] = None
    consume_interrupted = False
    for attempt in range(2):
        try:
            outcome = client.consume_reset(target.credit_id, attempt_key)
            uncertain_error = None
            break
        except KeyboardInterrupt:
            uncertain_error = GuardError(
                "interrupted",
                "Interrupted while awaiting the consume outcome.",
            )
            consume_interrupted = True
            break
        except (RpcTimeout, RpcTransportError, RpcProtocolError) as error:
            uncertain_error = error
            if attempt == 0:
                continue
        except Exception:
            uncertain_error = GuardError(
                "consume_internal_error",
                "An unexpected error occurred while awaiting the consume outcome.",
            )
            if attempt == 0:
                continue

    if outcome is None:
        # Fix the irreversible facts before any best-effort reconciliation or
        # rendering. No later exception may turn this ambiguity into applied=false.
        payload.update(
            ok=False,
            status="consume_outcome_unknown",
            applied=None,
            provider_outcome="unknown",
            error="consume_outcome_unknown",
            consume_error=(
                uncertain_error.code
                if uncertain_error is not None
                else "app_server_transport_error"
            ),
            pending_attempt_preserved=pending_store is not None,
            message=(
                "The consume request may have reached the provider, but no definitive "
                "outcome was received."
            ),
        )
        try:
            verification = (
                VerificationResult(
                    snapshot=None,
                    verified=False,
                    attempts=0,
                    last_error="verification_interrupted",
                )
                if consume_interrupted
                else read_verification_snapshot(client, before, target)
            )
            add_verification_to_payload(
                payload,
                verification,
                time.time(),
                within_hours,
            )
        except KeyboardInterrupt:
            payload.update(
                verified=False,
                verification_attempts=0,
                verification_error="verification_interrupted",
            )
        except Exception:
            payload.update(
                verified=False,
                verification_attempts=0,
                verification_error="verification_internal_error",
            )
        return payload

    payload["provider_outcome"] = outcome
    verification: Optional[VerificationResult] = None
    try:
        verification = read_verification_snapshot(
            client,
            before,
            target,
            attempts=3 if outcome in SUCCESS_OUTCOMES else 1,
            delay_seconds=0.25 if outcome in SUCCESS_OUTCOMES else 0,
        )
        add_verification_to_payload(payload, verification, time.time(), within_hours)
    except KeyboardInterrupt:
        verification = None
        payload.update(
            verified=False,
            verification_attempts=0,
            verification_error="verification_interrupted",
        )
    except Exception:
        verification = None
        payload.update(
            verified=False,
            verification_attempts=0,
            verification_error="verification_internal_error",
        )

    if outcome in SUCCESS_OUTCOMES:
        payload.update(
            status=(
                outcome
                if verification is not None and verification.verified
                else "provider_confirmed_verification_pending"
            ),
            applied=True,
        )
    elif outcome == "nothingToReset":
        payload.update(status="deferred_nothing_to_reset", applied=False)
    else:
        payload.update(status="no_credit", applied=False)

    # A confirmed success that has not converged is still the same logical
    # redemption attempt. Preserve its key so a later stale-detail retry cannot
    # create a second logical write.
    preserve_success_attempt = (
        outcome in SUCCESS_OUTCOMES
        and (verification is None or not verification.verified)
    )
    if preserve_success_attempt:
        if pending_store is not None:
            payload["pending_attempt_preserved"] = True
        return payload

    state_cleanup_error: Optional[GuardError] = None
    if pending_store is not None:
        try:
            pending_store.clear()
        except KeyboardInterrupt:
            payload.update(
                ok=False,
                status=(
                    "provider_confirmed_state_cleanup_pending"
                    if outcome in SUCCESS_OUTCOMES
                    else "provider_outcome_state_cleanup_pending"
                ),
                applied=outcome in SUCCESS_OUTCOMES,
                error="interrupted",
                state_error="interrupted",
                message=(
                    "The provider outcome is known, but local state cleanup was "
                    "interrupted."
                ),
            )
            return payload
        except GuardError as error:
            state_cleanup_error = error
        except Exception:
            state_cleanup_error = GuardError(
                "state_cleanup_internal_error",
                "An unexpected local state cleanup error occurred.",
            )

    if state_cleanup_error is not None:
        payload.update(
            ok=False,
            status=(
                "provider_confirmed_state_cleanup_pending"
                if outcome in SUCCESS_OUTCOMES
                else "provider_outcome_state_cleanup_pending"
            ),
            error=state_cleanup_error.code,
            state_error=state_cleanup_error.code,
            message=(
                "The provider outcome is known, but local pending state could not be "
                "cleared safely."
            ),
        )
    return payload

def human_lines(payload: Mapping[str, Any]) -> Iterable[str]:
    if not payload.get("ok"):
        yield "Codex reset guard failed: " + str(payload.get("error", "unknown_error"))
        if payload.get("status"):
            yield "Status: " + str(payload["status"])
        if "provider_outcome" in payload:
            yield "Provider outcome: " + str(payload["provider_outcome"])
        if "applied" in payload:
            applied = payload["applied"]
            yield "Applied: " + (
                "unknown" if applied is None else str(bool(applied)).lower()
            )
        if "verified" in payload:
            yield "Verified: " + str(bool(payload["verified"])).lower()
        if payload.get("pending_attempt_preserved"):
            yield "Pending attempt preserved: true"
        if payload.get("message"):
            yield str(payload["message"])
        return

    yield "Codex banked reset guard"
    yield ""
    yield "Status: " + str(payload.get("status", "unknown"))
    before = payload.get("before") if isinstance(payload.get("before"), Mapping) else payload
    if isinstance(before, Mapping):
        yield "Available: " + str(before.get("available_count"))

    selected = payload.get("selected_credit")
    if isinstance(selected, Mapping):
        yield "Selected: " + str(selected.get("fingerprint"))
        yield "Expires: " + str(selected.get("expires_at_local"))
    if payload.get("provider_outcome"):
        yield "Provider outcome: " + str(payload["provider_outcome"])
    if "applied" in payload:
        applied = payload["applied"]
        yield "Applied: " + (
            "unknown" if applied is None else str(bool(applied)).lower()
        )
    if "verified" in payload:
        yield "Verified: " + str(bool(payload["verified"])).lower()

    credits = before.get("credits") if isinstance(before, Mapping) else None
    if isinstance(credits, list) and credits:
        yield ""
        yield "| Credit | Status | Expires local | Remaining seconds |"
        yield "| --- | --- | --- | ---: |"
        for credit in credits:
            if isinstance(credit, Mapping):
                yield "| {} | {} | {} | {} |".format(
                    credit.get("fingerprint"),
                    credit.get("status"),
                    credit.get("expires_at_local"),
                    credit.get("remaining_seconds"),
                )


def error_payload(action: str, error: GuardError) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "tool_version": VERSION,
        "ok": False,
        "action": action,
        "error": error.code,
        "message": error.message,
    }


def output_serialization_error_payload(
    action: str,
    original: Mapping[str, Any],
) -> Dict[str, Any]:
    fallback = error_payload(
        action,
        GuardError(
            "output_serialization_error",
            "The sanitized result could not be encoded as strict JSON",
        ),
    )
    status = original.get("status")
    if type(status) is str and status in SAFE_FACT_STATUSES:
        fallback["status"] = status
    elif action == "guard":
        fallback["status"] = "output_serialization_error"

    provider_outcome = original.get("provider_outcome")
    if type(provider_outcome) is str and provider_outcome in KNOWN_OUTCOMES | {
        "unknown"
    }:
        fallback["provider_outcome"] = provider_outcome
    for field in (
        "apply_requested",
        "applied",
        "verified",
        "pending_attempt_preserved",
    ):
        value = original.get(field)
        if type(value) is bool or (field == "applied" and value is None):
            fallback[field] = value
    return fallback

def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check and safely redeem expiring Codex banked reset credits."
    )
    parser.add_argument("action", nargs="?", choices=("status", "guard"), default="status")
    parser.add_argument(
        "--within-hours",
        type=float,
        default=DEFAULT_WITHIN_HOURS,
        help="Guard window before expiry. Default: 1 hour.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually consume one due credit. Without this flag guard is a dry run.",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Print sanitized JSON. Default.")
    output.add_argument("--human", action="store_true", help="Print a compact human summary.")
    parser.add_argument("--codex-bin", default="codex", help="Codex CLI executable. Default: codex.")
    parser.add_argument("--timeout", type=float, default=20.0, help="RPC timeout in seconds.")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    if (
        not math.isfinite(args.within_hours)
        or args.within_hours <= 0
        or args.within_hours > 168
    ):
        parser.error("--within-hours must be finite, greater than 0, and no more than 168")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 120:
        parser.error("--timeout must be finite, greater than 0, and no more than 120")
    if args.apply and args.action != "guard":
        parser.error("--apply is valid only with the guard action")
    return args


def execute_action(
    args: argparse.Namespace,
    pending_store: Optional[PendingAttemptStore] = None,
) -> Dict[str, Any]:
    command = resolve_app_server_command(args.codex_bin)
    payload: Optional[Dict[str, Any]] = None
    try:
        with AppServerClient(command, timeout=args.timeout) as client:
            now = time.time()
            if args.action == "status":
                payload = run_status(client, now, args.within_hours)
            else:
                payload = run_guard(
                    client,
                    now,
                    args.within_hours,
                    args.apply,
                    pending_store=pending_store,
                )
    except KeyboardInterrupt:
        if payload is None:
            raise
        # The operation result was already known before process cleanup was
        # interrupted. Cleanup completed its hard fallback before re-raising.
        payload["process_cleanup_interrupted"] = True
    if payload is None:
        raise GuardError(
            "internal_error",
            "The guard did not produce a result.",
        )
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    payload: Optional[Dict[str, Any]] = None
    try:
        if args.action == "guard" and args.apply:
            state_directory = runtime_directory()
            apply_lock = NonBlockingApplyLock(state_directory / LOCK_FILENAME)
            pending_store = PendingAttemptStore(
                state_directory / PENDING_STATE_FILENAME
            )
            with apply_lock:
                payload = execute_action(args, pending_store=pending_store)
        else:
            payload = execute_action(args)
    except KeyboardInterrupt:
        if payload is not None:
            payload = dict(payload)
            payload.update(
                ok=False,
                error="lock_cleanup_interrupted",
                lock_cleanup_error="interrupted",
                message=(
                    "The operation result is known, but apply-lock cleanup was "
                    "interrupted."
                ),
            )
        else:
            payload = error_payload(
                args.action,
                GuardError("interrupted", "Interrupted by user"),
            )
            if args.action == "guard" and args.apply:
                payload.update(
                    status="interrupted_outcome_unknown",
                    applied=None,
                    message=(
                        "The apply run was interrupted; the consume outcome must be "
                        "treated as unknown."
                    ),
                )
    except GuardError as error:
        if payload is not None:
            payload = dict(payload)
            payload.update(
                ok=False,
                error=error.code,
                lock_cleanup_error=error.code,
                message=(
                    "The operation result is known, but apply-lock cleanup failed."
                ),
            )
        else:
            payload = error_payload(args.action, error)
    except Exception:
        if payload is not None:
            payload = dict(payload)
            payload.update(
                ok=False,
                error="lock_cleanup_internal_error",
                lock_cleanup_error="lock_cleanup_internal_error",
                message=(
                    "The operation result is known, but apply-lock cleanup failed."
                ),
            )
        else:
            payload = error_payload(
                args.action,
                GuardError("internal_error", "An unexpected internal error occurred."),
            )

    if payload is None:
        payload = error_payload(
            args.action,
            GuardError("internal_error", "The guard did not produce a result."),
        )
    if not payload.get("ok") and args.action == "guard":
        payload.setdefault("status", payload.get("error", "unknown_error"))
        payload.setdefault("apply_requested", bool(args.apply))
        payload.setdefault("applied", False)
        payload.setdefault("guard_window_hours", args.within_hours)

    if args.human:
        print("\n".join(human_lines(payload)))
    else:
        try:
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError):
            payload = output_serialization_error_payload(args.action, payload)
            rendered = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
        print(rendered)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
