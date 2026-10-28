"""Create an optional demo account on a fresh (ephemeral) database.

Runs before the server starts. Does nothing unless DEMO_EMAIL and DEMO_PASSWORD
are set (set them as Space secrets). Safe to run repeatedly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth.security import hash_password  # noqa: E402
from backend.database import ResearchRepository  # noqa: E402


def main() -> int:
    email = os.environ.get("DEMO_EMAIL", "").strip().lower()
    password = os.environ.get("DEMO_PASSWORD", "")
    if not email or not password:
        print("seed_demo: DEMO_EMAIL / DEMO_PASSWORD not set; skipping")
        return 0
    repo = ResearchRepository()
    repo.initialize()
    if repo.users.get_by_email(email):
        print("seed_demo: demo account already exists")
        return 0
    password_hash, salt = hash_password(password)
    repo.users.create(
        email=email,
        name=os.environ.get("DEMO_NAME", "Demo User"),
        password_hash=password_hash,
        password_salt=salt,
    )
    print("seed_demo: demo account created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
