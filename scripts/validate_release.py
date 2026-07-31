#!/usr/bin/env python3
"""Validate the repository's distributable Codex Skill without dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "skills"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise ValueError(message)


def frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 5 or lines[0] != "---":
        fail(f"{path}: missing YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"{path}: unterminated YAML frontmatter") from error

    values: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-z_]+):\s*(.+)", line)
        if match is None:
            fail(f"{path}: unsupported frontmatter line: {line!r}")
        key, value = match.groups()
        if key in values:
            fail(f"{path}: duplicate frontmatter key {key!r}")
        if value[:1] in {'"', "'"} and value[-1:] != value[:1]:
            fail(f"{path}: unmatched frontmatter quote for {key!r}")
        values[key] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[closing + 1 :]).strip()
    return values, body


def parse_agent_metadata(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'interface:\n'
        r'  display_name: "([^"\n]+)"\n'
        r'  short_description: "([^"\n]+)"\n'
        r'  default_prompt: "([^"\n]+)"\n?'
    )
    match = pattern.fullmatch(text)
    if match is None:
        fail("agents/openai.yaml must contain exactly the three quoted interface fields")
    return dict(zip(("display_name", "short_description", "default_prompt"), match.groups()))


def validate_one_hour_contract(readme: str, skill_text: str, script_text: str) -> None:
    docs = readme + "\n" + skill_text
    commands = re.findall(r"--within-hours\s+([^\s]+)", docs)
    if not commands:
        fail("documentation must include an explicit --within-hours 1 command")
    for token in commands:
        if token.rstrip(".,)" + chr(96)) != "1":
            fail(f"documentation contains a non-one-hour guard: --within-hours {token}")
    if re.search(r"\b(?:3|4)[ -]*(?:h|hr|hrs|hour|hours)\b|\b(?:three|four)[ -]?hours?\b", docs, flags=re.IGNORECASE):
        fail("documentation still contains a legacy guard-window reference")
    if "DEFAULT_WITHIN_HOURS = 1.0" not in script_text:
        fail("runtime default is not exactly one hour")


def validate_result_contract(readme: str, skill_text: str, script_text: str) -> None:
    statuses = (
        "checked",
        "not_due",
        "dry_run_due",
        "reset",
        "alreadyRedeemed",
        "provider_confirmed_verification_pending",
        "deferred_nothing_to_reset",
        "no_credit",
        "previous_attempt_reconciled_target_absent",
        "already_running",
        "consume_outcome_unknown",
        "interrupted_outcome_unknown",
        "provider_confirmed_state_cleanup_pending",
        "provider_outcome_state_cleanup_pending",
        "previous_attempt_reconciled_state_cleanup_pending",
        "reset_credits_unavailable",
        "credit_details_unavailable",
        "credit_details_incomplete",
        "pending_state_invalid",
        "pending_state_conflict",
        "pending_target_not_actionable",
        "guard_lock_unavailable",
        "runtime_state_unavailable",
        "unsafe_codex_batch_path",
    )
    for status in statuses:
        if status not in script_text:
            fail(f"documented status is absent from runtime: {status}")
        if status not in readme or status not in skill_text:
            fail(f"runtime status is missing from README or SKILL.md: {status}")


def validate_workflow(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    action_refs = re.findall(r"^\s*uses:\s*[^@\s]+@([^\s#]+)", text, flags=re.MULTILINE)
    if not action_refs or any(SHA_PATTERN.fullmatch(ref) is None for ref in action_refs):
        fail("every third-party GitHub Action must be pinned to a full lowercase commit SHA")
    for required in (
        "ubuntu-24.04",
        "windows-2025",
        "macos-15-intel",
        'python-version: ["3.9", "3.14"]',
        "python scripts/validate_release.py",
        "permissions:\n  contents: read",
        "persist-credentials: false",
    ):
        if required not in text:
            fail(f"workflow is missing release requirement: {required!r}")


def validate() -> None:
    skill_files = sorted(SKILLS_ROOT.rglob("SKILL.md"))
    if len(skill_files) != 1:
        fail(f"expected exactly one discoverable SKILL.md; found {len(skill_files)}")

    skill_file = skill_files[0]
    metadata, skill_body = frontmatter(skill_file)
    if set(metadata) != {"name", "description"}:
        fail("SKILL.md frontmatter must contain only name and description")

    name = metadata["name"]
    if len(name) > 64 or NAME_PATTERN.fullmatch(name) is None:
        fail(f"invalid Skill name: {name!r}")
    if skill_file.parent.name != name:
        fail("Skill folder name must equal the SKILL.md name")
    if not metadata["description"].strip() or not skill_body:
        fail("Skill description and body must not be empty")

    agent_file = skill_file.parent / "agents" / "openai.yaml"
    agent = parse_agent_metadata(agent_file)
    if not 25 <= len(agent["short_description"]) <= 64:
        fail("short_description must be 25-64 characters")
    prompt = agent["default_prompt"]
    prompt_lower = prompt.lower()
    if "$" + name not in prompt:
        fail("default_prompt must invoke the packaged Skill by name")
    if "--apply" in prompt or not (
        "do not redeem" in prompt_lower or "without redeem" in prompt_lower
    ):
        fail("default_prompt must explicitly remain read-only")

    script = skill_file.parent / "scripts" / "codex_banked_reset_guard.py"
    if not script.is_file():
        fail("bundled runtime script is missing")
    script_text = script.read_text(encoding="utf-8")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skill_text = skill_file.read_text(encoding="utf-8")
    if agent["default_prompt"] not in readme:
        fail("README default prompt does not match agents/openai.yaml")
    documented_statuses = (
        "checked",
        "not_due",
        "dry_run_due",
        "reset",
        "alreadyRedeemed",
        "provider_confirmed_verification_pending",
        "deferred_nothing_to_reset",
        "no_credit",
        "previous_attempt_reconciled_target_absent",
        "already_running",
        "consume_outcome_unknown",
        "interrupted_outcome_unknown",
        "provider_confirmed_state_cleanup_pending",
        "provider_outcome_state_cleanup_pending",
        "previous_attempt_reconciled_state_cleanup_pending",
        "pending_state_invalid",
        "pending_state_conflict",
        "pending_target_not_actionable",
        "guard_lock_unavailable",
        "runtime_state_unavailable",
        "unsafe_codex_batch_path",
    )
    for status in documented_statuses:
        if status not in script_text or status not in readme or status not in skill_text:
            fail(f"runtime/README/Skill status contract is missing {status!r}")
    owner_placeholder = "OWNER" + "/"
    repository_placeholder = "github.com/" + "OWNER"
    if owner_placeholder in readme or repository_placeholder in readme:
        fail("README contains an unresolved GitHub owner placeholder")
    validate_one_hour_contract(readme, skill_text, script_text)
    validate_result_contract(readme, skill_text, script_text)
    validate_workflow(ROOT / ".github" / "workflows" / "test.yml")


def main() -> int:
    try:
        validate()
    except (OSError, UnicodeError, ValueError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    print("release validation passed: 1 discoverable Skill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
