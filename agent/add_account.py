#!/usr/bin/env python3
"""Add or update an account in accounts.json.

Usage:
    python3 add_account.py USERNAME ACCOUNT_NAME

ACCOUNT_NAME is the logical group for this user's tasks (e.g. 'personal', 'test').
Multiple usernames can share the same account name — they will see the same tasks.

Example:
    python3 add_account.py xingliu personal
    python3 add_account.py friend  test
"""
import getpass
import json
import sys
from pathlib import Path
from werkzeug.security import generate_password_hash

ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    username = sys.argv[1]
    account = sys.argv[2]
    password = getpass.getpass(f"Password for '{username}': ")
    if not password:
        print("Error: password cannot be empty.")
        sys.exit(1)

    accounts = {}
    if ACCOUNTS_FILE.exists():
        accounts = json.loads(ACCOUNTS_FILE.read_text())

    accounts[username] = {
        "password_hash": generate_password_hash(password),
        "account": account,
    }

    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2))
    print(f"Saved: '{username}' → account '{account}'")


if __name__ == "__main__":
    main()
