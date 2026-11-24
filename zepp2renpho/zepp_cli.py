"""`zepp-export`: copy the whole Zepp scale history to a folder.

    zepp-export --out export/
    zepp-export members

The export writes `<out>/members.json` and `<out>/records.jsonl`, the two
files the RENPHO importer reads.

Credentials, in this order:

1. `--app-token` and `--user-id`, or `ZEPP_APP_TOKEN` and `ZEPP_USER_ID`.
   Use this path for an account created with Google or Apple sign in: that
   account has no password, so only a captured token works. Read the token
   the way huami-token documents it: log in once in the Zepp app through a
   HTTPS proxy (mitmproxy with its certificate installed on the phone) and
   copy the `apptoken` header and the `userid` query parameter of any call.
2. `--email` plus `ZEPP_PASSWORD`, or a password typed at the prompt.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path

from .records import Member, Record, write_members, write_records
from .zepp import (
    DEFAULT_API_HOST,
    DebugDump,
    ExportStats,
    ZeppAuth,
    ZeppClient,
    apply_baby_flags,
    collect_records,
    login,
    members_with_owner,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command line of `zepp-export`."""
    parser = argparse.ArgumentParser(
        prog="zepp-export",
        description="Export the Zepp scale history of the owner and the family members.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="export",
        choices=("export", "members"),
        help="export writes the files, members only prints the member list",
    )
    parser.add_argument("--out", default="export", help="output folder (default: export)")
    parser.add_argument("--email", help="Zepp account email, for password login")
    parser.add_argument("--app-token", help="app token captured from the Zepp app")
    parser.add_argument("--user-id", help="Zepp numeric user id that goes with the app token")
    parser.add_argument(
        "--api-host",
        help="override the data host, for example api-mifit-de2.zepp.com",
    )
    parser.add_argument(
        "--baby-member",
        action="append",
        default=[],
        metavar="ID",
        help="mark this member id as a baby, repeat for several members",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="dump every raw API answer, tokens redacted, into <out>/debug",
    )
    return parser


def read_password() -> str:
    """Read the password from the environment or from the terminal."""
    password = os.environ.get("ZEPP_PASSWORD")
    if password:
        return password
    return getpass("Zepp password: ")


def build_auth(args: argparse.Namespace, debug: DebugDump | None) -> ZeppAuth:
    """Return the credentials, either from the token flags or from a login."""
    app_token = args.app_token or os.environ.get("ZEPP_APP_TOKEN")
    user_id = args.user_id or os.environ.get("ZEPP_USER_ID")
    if app_token and user_id:
        return ZeppAuth(app_token=app_token, user_id=user_id, api_host=_token_host(args))
    email = args.email or os.environ.get("ZEPP_EMAIL")
    if not email:
        raise SystemExit(
            "Give --app-token and --user-id, or --email. "
            "A Google or Apple account has no password: use the token."
        )
    return login(email, read_password(), api_host=args.api_host, debug=debug)


def _token_host(args: argparse.Namespace) -> str:
    """Pick the data host for the token path, where no login names one."""
    return args.api_host or os.environ.get("ZEPP_API_HOST") or DEFAULT_API_HOST


def build_debug(args: argparse.Namespace) -> DebugDump | None:
    """Create the debug folder when `--debug` is set."""
    if not args.debug:
        return None
    return DebugDump(Path(args.out) / "debug")


def format_day(timestamp: int) -> str:
    """Format an epoch second as a UTC date."""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")


def member_line(member: Member, records: list[Record]) -> str:
    """One report line: id, name, baby flag, count, first and last date."""
    own = sorted(record.timestamp for record in records if record.member_id == member.member_id)
    span = f"{format_day(own[0])} .. {format_day(own[-1])}" if own else "no records"
    baby = {True: "baby", False: "adult", None: "unknown"}[member.is_baby]
    return f"{member.member_id:>12}  {member.name:<20} {baby:<7} {len(own):>6}  {span}"


def print_members(members: list[Member], records: list[Record]) -> None:
    """Print the member table of the export."""
    print(f"{'member id':>12}  {'name':<20} {'kind':<7} {'count':>6}  dates")
    for member in members:
        print(member_line(member, records))


def run_members(client: ZeppClient, args: argparse.Namespace) -> int:
    """`zepp-export members`: print the member list and stop."""
    members = apply_baby_flags(members_with_owner(client), args.baby_member)
    print_members(members, [])
    return 0


def run_export(client: ZeppClient, args: argparse.Namespace) -> int:
    """`zepp-export`: write the member list and the full record history."""
    members = members_with_owner(client)
    collector, baby_ids = collect_records(client, members)
    members = apply_baby_flags(members, baby_ids | set(args.baby_member))
    records = collector.sorted_records()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_members(out / "members.json", members)
    write_records(out / "records.jsonl", records)
    print_members(members, records)
    print(_stats_line(collector.stats, len(records), out))
    return 0


def _stats_line(stats: ExportStats, written: int, out: Path) -> str:
    """Summarise what was read and what was dropped."""
    return (
        f"\n{written} records written to {out / 'records.jsonl'}, "
        f"{stats.fetched} rows read, {stats.duplicates} duplicates, "
        f"{stats.invalid} without a usable weight, "
        f"{stats.skipped_imported} copied by Zepp from another app"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point of `zepp-export`."""
    args = build_parser().parse_args(argv)
    debug = build_debug(args)
    auth = build_auth(args, debug)
    client = ZeppClient(auth, debug=debug)
    client.verify_token()
    if args.command == "members":
        return run_members(client, args)
    return run_export(client, args)


if __name__ == "__main__":
    sys.exit(main())
