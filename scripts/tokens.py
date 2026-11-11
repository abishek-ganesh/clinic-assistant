"""Mint, list and revoke student tokens.

uv run python scripts/tokens.py mint --label "Ada Lovelace" --email ada@example.com
uv run python scripts/tokens.py mint --roster roster.csv > ~/tokens.csv   (columns: label,email)
uv run python scripts/tokens.py list
uv run python scripts/tokens.py revoke PREFIX_OR_LABEL
uv run python scripts/tokens.py class-code new        (prints a new code; share it in Slack)
uv run python scripts/tokens.py class-code set CODE   (use a code of your own)
uv run python scripts/tokens.py class-code close      (stop the /token page issuing tokens)

A plain token is printed once, at mint time. Only its hash is stored, so it cannot be shown again.
"""

import argparse
import asyncio
import csv
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.access import hash_class_code, mint_token  # noqa: E402
from app.db import get_db  # noqa: E402


async def mint(people: list[dict]) -> list[dict]:
    """Insert one token row per person and return label, email and the plain token."""
    rows, minted = [], []
    for person in people:
        token, token_hash, prefix = mint_token()
        rows.append(
            {
                "label": person["label"],
                "email": person["email"],
                "token_hash": token_hash,
                "token_prefix": prefix,
            }
        )
        minted.append({"label": person["label"], "email": person["email"] or "", "token": token})
    db = await get_db()
    await db.table("student_tokens").insert(rows).execute()
    return minted


def read_roster(path: str) -> list[dict]:
    """Read label,email rows from a CSV file. email may be blank."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "label" not in reader.fieldnames:
            sys.exit(f"{path} needs a header row with the columns label,email")
        people = [
            {"label": row["label"].strip(), "email": (row.get("email") or "").strip() or None}
            for row in reader
        ]
    if not people or any(not person["label"] for person in people):
        sys.exit(f"{path} has no rows, or a row with an empty label")
    return people


async def cmd_mint(args: argparse.Namespace) -> None:
    if args.roster:
        minted = await mint(read_roster(args.roster))
        writer = csv.DictWriter(sys.stdout, fieldnames=["label", "email", "token"])
        writer.writeheader()
        writer.writerows(minted)
        print(f"Minted {len(minted)} tokens. They cannot be shown again.", file=sys.stderr)
    else:
        [minted] = await mint([{"label": args.label, "email": args.email}])
        print(minted["token"])
        print(f"Token for {args.label}. It cannot be shown again.", file=sys.stderr)


async def cmd_list(_args: argparse.Namespace) -> None:
    db = await get_db()
    tokens = (await db.table("student_tokens").select("*").order("created_at").execute()).data
    # "Today" matches the daily limit in authorize_request: class time (ET), denials excluded
    eastern = ZoneInfo("America/New_York")
    day_start = datetime.now(eastern).replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"{'label':<28} {'email':<32} {'prefix':<20} {'created':<12} {'revoked':<12} today")
    for token in tokens:
        counted = await (
            db.table("usage_log")
            .select("id", count="exact", head=True)
            .eq("token_id", token["id"])
            .neq("outcome", "denied")
            .gte("created_at", day_start.isoformat())
            .execute()
        )
        created = datetime.fromisoformat(token["created_at"]).astimezone(eastern).date()
        revoked = token["revoked_at"]
        revoked = datetime.fromisoformat(revoked).astimezone(eastern).date() if revoked else "-"
        print(
            f"{token['label']:<28} {token['email'] or '-':<32} {token['token_prefix']:<20} "
            f"{created!s:<12} {revoked!s:<12} {counted.count}"
        )


async def cmd_revoke(args: argparse.Namespace) -> None:
    db = await get_db()
    active = (
        await db.table("student_tokens")
        .select("id, label, token_prefix")
        .is_("revoked_at", "null")
        .execute()
    ).data
    matches = [t for t in active if args.target in (t["token_prefix"], t["label"])]
    if not matches:
        sys.exit(f"No active token has the prefix or label {args.target!r}")
    if len(matches) > 1:
        found = ", ".join(f"{t['label']} ({t['token_prefix']})" for t in matches)
        sys.exit(f"{args.target!r} matches {len(matches)} tokens: {found}. Revoke by prefix.")
    [token] = matches
    now = datetime.now(UTC).isoformat()
    await db.table("student_tokens").update({"revoked_at": now}).eq("id", token["id"]).execute()
    print(f"Revoked {token['label']} ({token['token_prefix']})")


def new_class_code() -> str:
    """A code that is easy to read aloud and far too long to guess: clinic-xxxx-xxxx-xxxx."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes such as l/1 or o/0
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "clinic-" + "-".join(groups)


async def cmd_class_code(args: argparse.Namespace) -> None:
    """Set or clear the code the /token page asks for. Only its hash is stored."""
    if args.action == "set" and not args.code:
        sys.exit("class-code set needs the code: class-code set CODE")
    code = {"new": new_class_code(), "set": args.code, "close": None}[args.action]
    db = await get_db()
    value = hash_class_code(code) if code else None
    await db.table("app_settings").upsert({"key": "class_code_hash", "value": value}).execute()
    if args.action == "new":
        print(code)
        print("New class code set. Tokens already issued keep working.", file=sys.stderr)
    elif args.action == "set":
        print("Class code set. Tokens already issued keep working.", file=sys.stderr)
    else:
        print("Token requests are closed. Tokens already issued keep working.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint, list and revoke student tokens.")
    commands = parser.add_subparsers(dest="command", required=True)

    mint_parser = commands.add_parser("mint", help="create one token, or one per roster row")
    source = mint_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--label", help="who the token is for")
    source.add_argument("--roster", help="CSV file with the columns label,email")
    mint_parser.add_argument("--email", help="used with --label")
    mint_parser.set_defaults(run=cmd_mint)

    list_parser = commands.add_parser("list", help="show every token and today's request count")
    list_parser.set_defaults(run=cmd_list)

    revoke_parser = commands.add_parser("revoke", help="turn off one token")
    revoke_parser.add_argument("target", metavar="PREFIX_OR_LABEL")
    revoke_parser.set_defaults(run=cmd_revoke)

    code_parser = commands.add_parser("class-code", help="set the code the /token page asks for")
    code_parser.add_argument("action", choices=["new", "set", "close"])
    code_parser.add_argument("code", nargs="?")
    code_parser.set_defaults(run=cmd_class_code)

    args = parser.parse_args()
    asyncio.run(args.run(args))


if __name__ == "__main__":
    main()
