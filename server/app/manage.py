"""Phase 1a: account administration from the command line (works in production, where no
demo users exist). Run from server/ with the same environment as the API:

    python -m app.manage create-user --username jsmith --name "Jane Smith" --role teacher
    python -m app.manage set-password --username jsmith
    python -m app.manage revoke-tokens --username jsmith      # sign out everywhere
    python -m app.manage unlock-login --username jsmith       # clear a sign-in lockout

Passwords are prompted (twice, not echoed) or read from stdin with --password-stdin.
`--role` is passed through as given; it must be a role the token code accepts.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from .config import Settings
from .db import Database, set_db
from .models import User
from .security import hash_password
from .store import make_store, set_store

MIN_PASSWORD_LEN = 8


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Password: ")
        if getpass.getpass("Repeat password: ") != pw:
            raise SystemExit("Passwords do not match")
    if len(pw) < MIN_PASSWORD_LEN:
        raise SystemExit(f"Password must have at least {MIN_PASSWORD_LEN} characters")
    return pw


async def _run(args: argparse.Namespace, settings: Settings) -> str:
    db = Database(settings)
    await db.create_all()
    set_db(db)
    store = make_store(settings.redis_url)
    set_store(store)
    try:
        async with db.sessionmaker() as session:
            username = args.username.strip()
            user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
            if args.command == "create-user":
                if user is not None:
                    raise SystemExit(f"User {username!r} already exists")
                pw = _read_password(args.password_stdin)
                session.add(User(username=username, name=args.name or username, role=args.role,
                                 password_hash=hash_password(pw, settings.pbkdf2_iterations)))
                await session.commit()
                return f"Created {args.role} {username!r}"
            if user is None:
                raise SystemExit(f"No user {username!r}")
            if args.command == "set-password":
                pw = _read_password(args.password_stdin)
                user.password_hash = hash_password(pw, settings.pbkdf2_iterations)
                from .account import revoke_user_tokens
                await revoke_user_tokens(session, user.id)
                return f"Password changed for {username!r}; existing sign-ins revoked"
            if args.command == "revoke-tokens":
                from .account import revoke_user_tokens
                ver = await revoke_user_tokens(session, user.id)
                return f"Signed {username!r} out everywhere (token version {ver})"
            if args.command == "unlock-login":
                from .hardening import unlock_login
                await unlock_login(store, username)
                return f"Sign-in lockout cleared for {username!r}"
            raise SystemExit(f"Unknown command {args.command}")
    finally:
        await store.close()
        await db.dispose()
        set_db(None)
        set_store(None)


def main(argv: list[str] | None = None, settings: Settings | None = None) -> str:
    ap = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create-user", help="create an account (prompts for the password)")
    c.add_argument("--username", required=True)
    c.add_argument("--name", default="")
    c.add_argument("--role", required=True, help="e.g. teacher, student (passed through)")
    for name in ("set-password", "revoke-tokens", "unlock-login"):
        sub.add_parser(name).add_argument("--username", required=True)
    for p in (c, sub.choices["set-password"]):
        p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    args = ap.parse_args(argv)
    msg = asyncio.run(_run(args, settings or Settings()))
    print(msg)
    return msg


if __name__ == "__main__":
    main()
