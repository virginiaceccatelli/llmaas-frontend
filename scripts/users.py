"""
Manage the frontend's user file.

    python scripts/users.py add alice              # prompts for a password
    python scripts/users.py add alice --email a@b.c
    python scripts/users.py list
    python scripts/users.py remove alice

The file only exists to gate sign-in. The moment it exists, passwordless dev
sign-in stops working — which is the point. Passwords are hashed with scrypt;
plaintext is never written anywhere.
"""
import argparse
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.users import USER_ID_RE, hash_password  # noqa: E402


def load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="users.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="add or update a user")
    add.add_argument("user_id")
    add.add_argument("--email", default="")
    add.add_argument("--password", help="skip the prompt (shows in shell history)")

    sub.add_parser("list", help="list user ids")

    rm = sub.add_parser("remove", help="remove a user")
    rm.add_argument("user_id")

    args = ap.parse_args()
    path = Path(args.file)
    users = load(path)

    if args.cmd == "list":
        if not users:
            print(f"{path}: no users (passwordless dev sign-in applies)")
            return 0
        for uid, rec in sorted(users.items()):
            print(f"{uid:<24} {rec.get('email', '')}")
        return 0

    if args.cmd == "remove":
        if users.pop(args.user_id, None) is None:
            print(f"no such user: {args.user_id}", file=sys.stderr)
            return 1
        save(path, users)
        print(f"removed {args.user_id}")
        return 0

    # add
    if not USER_ID_RE.match(args.user_id):
        print("user id must be 1-64 chars: letters, digits, and . _ - @", file=sys.stderr)
        return 2
    password = args.password or getpass.getpass("password: ")
    if not args.password and password != getpass.getpass("again: "):
        print("passwords do not match", file=sys.stderr)
        return 2
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        return 2

    users[args.user_id] = {"password_hash": hash_password(password), "email": args.email}
    save(path, users)
    print(f"saved {args.user_id} to {path}")
    print("Password login is now required — passwordless dev sign-in is off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
