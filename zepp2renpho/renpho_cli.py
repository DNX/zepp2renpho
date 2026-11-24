"""Command line front end that uploads an exported Zepp history into RENPHO.

Commands: `members`, `mapping-template`, `plan`, `test`, `import`.
Read the module docstring of `renpho.py` for the template rule and the risks.

The password comes from the `RENPHO_PASSWORD` environment variable, or from an
interactive prompt. It is never printed, logged, or written to a debug dump.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from renpho.exceptions import RenphoAPIError

from .records import Member, Record, read_members, read_records
from .renpho import (
    BATCH_PAUSE_SECONDS,
    BATCH_SIZE,
    DEFAULT_TIMEZONE,
    MemberPlan,
    MissingTemplateError,
    Profile,
    RenphoApi,
    RenphoImportError,
    Row,
    Template,
    borrowed_template,
    build_payloads,
    build_template,
    classify,
    load_mapping,
    mapping_skeleton,
    resolve_pairs,
    row_matches,
)

CONFIRMATION = "UPLOAD"


# ----- argument parsing -----


def build_parser() -> argparse.ArgumentParser:
    """Build the full command line, one subcommand per action."""
    parser = argparse.ArgumentParser(
        prog="renpho-import", description="Upload an exported Zepp history into RENPHO Health."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add_members(commands)
    add_mapping_template(commands)
    add_plan(commands)
    add_test(commands)
    add_import(commands)
    return parser


def add_account_options(parser: argparse.ArgumentParser) -> None:
    """Options every command that signs in needs."""
    parser.add_argument(
        "--email",
        default=os.environ.get("RENPHO_EMAIL"),
        required=not os.environ.get("RENPHO_EMAIL"),
        help="RENPHO account email (default: RENPHO_EMAIL)",
    )
    parser.add_argument("--debug", type=Path, help="Folder for raw API dumps (tokens redacted)")


def add_data_options(parser: argparse.ArgumentParser) -> None:
    """Options every command that reads the export needs."""
    parser.add_argument("--export", type=Path, required=True, help="Folder written by zepp-export")
    parser.add_argument("--mapping", type=Path, required=True, help="Zepp to RENPHO mapping file")
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Zone for readings without one (default {DEFAULT_TIMEZONE})",
    )
    parser.add_argument(
        "--borrow-owner-template",
        action="store_true",
        help="For a profile without readings, send weight only with the owner's scale identity",
    )


def add_members(commands) -> None:
    """`members`: list the destination profiles and their templates."""
    parser = commands.add_parser("members", help="List RENPHO profiles and template status")
    add_account_options(parser)
    parser.set_defaults(handler=run_members)


def add_mapping_template(commands) -> None:
    """`mapping-template`: write the skeleton mapping file."""
    parser = commands.add_parser("mapping-template", help="Write an empty mapping file")
    parser.add_argument("--export", type=Path, required=True, help="Folder written by zepp-export")
    parser.add_argument("--mapping", type=Path, required=True, help="File to write")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing mapping file")
    parser.set_defaults(handler=run_mapping_template)


def add_plan(commands) -> None:
    """`plan`: read-only comparison of the export against RENPHO."""
    parser = commands.add_parser("plan", help="Compare the export with RENPHO, upload nothing")
    add_account_options(parser)
    add_data_options(parser)
    parser.add_argument("--show-payload", type=int, metavar="N", help="Print N example payloads")
    parser.set_defaults(handler=run_plan)


def add_test(commands) -> None:
    """`test`: upload a single reading and read it back."""
    parser = commands.add_parser("test", help="Upload one reading and verify it")
    add_account_options(parser)
    add_data_options(parser)
    parser.add_argument("--account-id", required=True, help="Numeric account id from `members`")
    parser.add_argument("--member", required=True, help="Zepp member id to test")
    parser.add_argument("--at", help="Local time of the reading to send, YYYY-MM-DD HH:MM:SS")
    parser.set_defaults(handler=run_test)


def add_import(commands) -> None:
    """`import`: upload every missing reading in batches."""
    parser = commands.add_parser("import", help="Upload the missing readings")
    add_account_options(parser)
    add_data_options(parser)
    parser.add_argument("--account-id", required=True, help="Numeric account id from `members`")
    parser.add_argument("--member", help="Import only this Zepp member id")
    parser.add_argument("--limit", type=int, help="Upload at most this many readings per member")
    parser.set_defaults(handler=run_import)


def validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject argument combinations argparse cannot express."""
    account_id = getattr(args, "account_id", None)
    if account_id is not None and not account_id.isdecimal():
        parser.error("--account-id must be the numeric account id printed by `members`")
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit must be a positive count")
    if getattr(args, "show_payload", None) is not None and args.show_payload < 1:
        parser.error("--show-payload must be a positive count")


# ----- shared steps -----


def read_password() -> str:
    """Take the password from the environment, or ask for it once."""
    password = os.environ.get("RENPHO_PASSWORD")
    if password:
        return password
    return getpass.getpass("RENPHO password: ")


def connect(args: argparse.Namespace) -> RenphoApi:
    """Sign in and return the API wrapper."""
    from renpho import RenphoClient

    client = RenphoClient(args.email, read_password())
    api = RenphoApi(client, debug_dir=getattr(args, "debug", None))
    api.login(expected_account_id=getattr(args, "account_id", None))
    print(f"Signed in to RENPHO account {api.account_id}.", flush=True)
    return api


def load_zone(name: str) -> ZoneInfo:
    """Read the fallback time zone for records that carry none."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RenphoImportError(f"Unknown time zone: {name}") from None


def load_export(folder: Path) -> tuple[list[Member], dict[str, list[Record]]]:
    """Read `members.json` and `records.jsonl` written by the exporter."""
    members_file, records_file = folder / "members.json", folder / "records.jsonl"
    for path in (members_file, records_file):
        if not path.is_file():
            raise RenphoImportError(f"Missing {path}. Run zepp-export first.")
    grouped: dict[str, list[Record]] = defaultdict(list)
    for record in read_records(records_file):
        grouped[record.member_id].append(record)
    return read_members(members_file), dict(grouped)


def member_plans(api: RenphoApi, args: argparse.Namespace) -> list[MemberPlan]:
    """Compare every mapped member with the matching RENPHO profile."""
    members, records = load_export(args.export)
    mapping = load_mapping(args.mapping)
    pairs, skipped = resolve_pairs(mapping, members, api.profiles())
    report_skipped(skipped)
    zone = load_zone(args.timezone)
    selected = getattr(args, "member", None)
    plans = []
    for member, profile in pairs:
        if selected and member.member_id != selected:
            continue
        plans.append(
            plan_for(
                api, member, profile, records.get(member.member_id, []), zone,
                borrow=args.borrow_owner_template,
            )
        )
    if selected and not plans:
        raise RenphoImportError(f"Zepp member {selected} is not mapped to a RENPHO profile.")
    return plans


def plan_for(
    api: RenphoApi,
    member: Member,
    profile: Profile,
    records: Sequence[Record],
    zone: ZoneInfo,
    *,
    borrow: bool = False,
) -> MemberPlan:
    """Build the plan for one member: template, payloads, classification."""
    history = api.history(profile.profile_id)
    template = template_for(api, profile, history, borrow)
    payloads = build_payloads(records, template, zone)
    plan = classify(payloads, history, profile, template)
    print_plan(member, plan)
    return plan


def template_for(
    api: RenphoApi, profile: Profile, history: dict[int, list[Row]], borrow: bool
) -> Template:
    """Use the profile's own newest reading, or borrow the owner's when asked."""
    try:
        return build_template(profile, flatten(history))
    except MissingTemplateError:
        if not borrow or profile.is_account_owner:
            raise
    owner = api.profiles()[0]
    owner_template = build_template(owner, flatten(api.history(owner.profile_id)))
    print(f"{profile.name}: no reading yet, borrowing the scale identity of {owner.name}.")
    return borrowed_template(profile, owner_template)


def flatten(history: dict[int, list[Row]]) -> list[Row]:
    """All stored rows of one profile, in no particular order."""
    return [row for rows in history.values() for row in rows]


def report_skipped(skipped: Sequence[tuple[Member, str]]) -> None:
    """Name every exported member that will not be imported."""
    for member, reason in skipped:
        print(f"Skipped {member.name} (Zepp id {member.member_id}): {reason}.")


def print_plan(member: Member, plan: MemberPlan) -> None:
    """Print the match, missing and conflict counts of one member."""
    profile = plan.profile
    kind = "baby" if profile.is_baby else "adult"
    print(
        f"{member.name} (Zepp {member.member_id}) -> {profile.name} "
        f"(RENPHO {profile.profile_id}, {kind}, deviceType {plan.template.device_type}): "
        f"{len(plan.matched)} matching, {len(plan.pending)} missing, {len(plan.conflicts)} conflicts."
    )
    for payload, fields in plan.conflicts:
        print(f"  conflict {payload['localCreatedAt']} ({', '.join(fields)})")


# ----- commands -----


def run_members(args: argparse.Namespace) -> int:
    """List every RENPHO profile with its reading count and template state."""
    api = connect(args)
    print(f"Account id: {api.account_id}")
    for profile in api.profiles():
        print(describe_profile(api, profile))
    print("Use the account id above as --account-id for test and import.")
    return 0


def describe_profile(api: RenphoApi, profile: Profile) -> str:
    """One line per profile: id, name, kind, readings, template state."""
    history = api.history(profile.profile_id)
    rows = [row for group in history.values() for row in group]
    kind = "baby" if profile.is_baby else "adult"
    owner = ", account owner" if profile.is_account_owner else ""
    head = f"  {profile.profile_id}  {profile.name} ({kind}{owner})  {len(rows)} readings  "
    try:
        template = build_template(profile, rows)
    except RenphoImportError as error:
        return head + f"template MISSING: {error}"
    return head + f"template ok (deviceType {template.device_type}, store {template.store})"


def run_mapping_template(args: argparse.Namespace) -> int:
    """Write an empty mapping file and print the member names it needs."""
    members, records = load_export(args.export)
    if args.mapping.exists() and not args.force:
        raise RenphoImportError(f"{args.mapping} exists. Pass --force to overwrite it.")
    skeleton = mapping_skeleton(members)
    args.mapping.write_text(json.dumps(skeleton, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"Wrote {args.mapping} with {len(skeleton)} empty entries.")
    print(f"{'Zepp id':<24} {'name':<24} {'kind':<6} readings")
    for member in members:
        kind = "baby" if member.is_baby else "adult"
        print(f"{member.member_id:<24} {member.name:<24} {kind:<6} {len(records.get(member.member_id, []))}")
    print("Fill each value with the RENPHO profile id from `renpho-import members`.")
    return 0


def run_plan(args: argparse.Namespace) -> int:
    """Compare the export with RENPHO and upload nothing."""
    api = connect(args)
    plans = member_plans(api, args)
    if args.show_payload:
        show_payloads(plans, args.show_payload)
    return 0


def show_payloads(plans: Sequence[MemberPlan], count: int) -> None:
    """Print the exact JSON of the first payloads that would be sent."""
    examples = [payload for plan in plans for payload in plan.pending][:count]
    if not examples:
        print("No missing readings, so there is no payload to show.")
        return
    print(f"Next {len(examples)} payload(s) as they would be sent:")
    print(json.dumps(examples, indent=2, ensure_ascii=False, sort_keys=True))


def run_test(args: argparse.Namespace) -> int:
    """Upload one missing reading, then read it back and verify it."""
    api = connect(args)
    check_account(api, args.account_id)
    plan = single_plan(member_plans(api, args))
    block_on_conflicts(plan)
    payload = select_test_payload(plan, args.at)
    print(f"Selected the reading of {payload['localCreatedAt']}.")
    if not confirmed():
        return 1
    upload_batch(api, plan, [payload], load_zone(args.timezone))
    print("Open RENPHO History and check that this reading looks right before importing the rest.")
    return 0


def select_test_payload(plan: MemberPlan, at: str | None) -> dict[str, Any]:
    """Pick the reading to test: the newest missing one, or the asked-for one."""
    if not plan.pending:
        raise RenphoImportError("Every exported reading of this member is already in RENPHO.")
    if not at:
        return plan.pending[-1]
    chosen = [p for p in plan.pending if p["localCreatedAt"] == at]
    if not chosen:
        raise RenphoImportError(f"--at {at} does not match a missing reading of this member.")
    return chosen[0]


def run_import(args: argparse.Namespace) -> int:
    """Upload the missing readings of every selected member, newest first."""
    api = connect(args)
    check_account(api, args.account_id)
    plans = member_plans(api, args)
    zone = load_zone(args.timezone)
    for plan in plans:
        import_member(api, plan, zone, args.limit)
    return 0


def import_member(api: RenphoApi, plan: MemberPlan, zone: ZoneInfo, limit: int | None) -> None:
    """Upload one member's missing readings in verified batches."""
    label = f"{plan.profile.name} (RENPHO {plan.profile.profile_id})"
    block_on_conflicts(plan)
    if not plan.pending:
        print(f"{label}: nothing to upload.")
        return
    if not plan.matched:
        raise RenphoImportError(
            f"{label}: no imported reading has been verified yet. Run "
            f"`renpho-import test --member <zepp id>` first and check it in the app."
        )
    queue = list(reversed(plan.pending))[:limit]
    print(f"{label}: uploading {len(queue)} reading(s), newest first.")
    if not confirmed():
        raise RenphoImportError("Stopped before uploading.")
    upload_queue(api, plan, queue, zone)


def upload_queue(
    api: RenphoApi, plan: MemberPlan, queue: Sequence[dict[str, Any]], zone: ZoneInfo
) -> None:
    """Send the queue in batches of ten, verifying after every batch."""
    for start in range(0, len(queue), BATCH_SIZE):
        batch = queue[start : start + BATCH_SIZE]
        upload_batch(api, plan, batch, zone)
        print(f"Verified {start + len(batch)} of {len(queue)} reading(s).", flush=True)
        if start + len(batch) < len(queue):
            time.sleep(BATCH_PAUSE_SECONDS)


def upload_batch(
    api: RenphoApi, plan: MemberPlan, batch: Sequence[dict[str, Any]], zone: ZoneInfo
) -> None:
    """Send one batch, wait for the cloud to settle, then read it back."""
    api.upload(batch, zone)
    time.sleep(BATCH_PAUSE_SECONDS)
    verify_batch(api, plan, batch)


def verify_batch(api: RenphoApi, plan: MemberPlan, batch: Sequence[dict[str, Any]]) -> None:
    """Fail on the first reading the cloud does not return as uploaded."""
    history = api.history(plan.profile.profile_id)
    for payload in batch:
        rows = history.get(payload["timeStamp"], [])
        if any(row_matches(payload, row, plan.template) for row in rows):
            continue
        raise RenphoImportError(
            f"Readback could not verify {payload['localCreatedAt']} for profile "
            f"{plan.profile.profile_id}. Check RENPHO History, then run plan again. "
            f"Resume with: renpho-import import --member <zepp id>"
        )


def single_plan(plans: Sequence[MemberPlan]) -> MemberPlan:
    """The test command works on exactly one member."""
    if len(plans) != 1:
        raise RenphoImportError("--member must select exactly one mapped Zepp member.")
    return plans[0]


def block_on_conflicts(plan: MemberPlan) -> None:
    """A different reading at the same second has to be resolved by hand."""
    if plan.conflicts:
        raise RenphoImportError(
            f"{plan.profile.name}: {len(plan.conflicts)} timestamp conflict(s). "
            "Resolve them in the RENPHO app before uploading."
        )


def check_account(api: RenphoApi, account_id: str) -> None:
    """Refuse to write when the signed-in account is not the expected one."""
    if api.account_id != account_id:
        raise RenphoImportError(
            f"Signed in as account {api.account_id}, but --account-id says {account_id}."
        )


def confirmed() -> bool:
    """Ask for the typed confirmation before any write."""
    return input(f"Type {CONFIRMATION} to proceed: ") == CONFIRMATION


# ----- entry point -----


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line, run the command, and turn failures into messages."""
    parser = build_parser()
    args = parser.parse_args(argv)
    validate(parser, args)
    try:
        return args.handler(args)
    except RenphoImportError as error:
        print(str(error), file=sys.stderr)
        return 1
    except RenphoAPIError as error:
        print(f"RENPHO refused the request. {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Stopped. Run plan before resuming.", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - the token must never reach the terminal
        print(describe_failure(error), file=sys.stderr)
        return 1


def describe_failure(error: Exception) -> str:
    """Describe an unexpected failure without showing request details."""
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == 429:
        retry = response.headers.get("Retry-After", "")
        wait = f" Wait at least {retry} seconds." if retry.isdecimal() else ""
        return "RENPHO rate-limited the request (HTTP 429)." + wait + " Run plan before resuming."
    return f"Stopped ({type(error).__name__}). Check RENPHO History and run plan before resuming."


if __name__ == "__main__":
    sys.exit(main())
