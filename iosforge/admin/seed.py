"""Seed / update an admin operator account (no public registration, SPEC §7).

Usage:
    ADMIN_SEED_USERNAME=op ADMIN_SEED_PASSWORD=... uv run python -m iosforge.admin.seed
    # or: uv run python -m iosforge.admin.seed <username> <password>

The password is read from env/args and stored only as an argon2 hash. Existing
accounts are updated (password reset) rather than duplicated.
"""

from __future__ import annotations

import sys

from sqlalchemy import select

from iosforge.admin.security import hash_password
from iosforge.common.config import get_settings
from iosforge.db.models import AdminUser
from iosforge.db.session import get_sessionmaker


def seed(username: str, password: str) -> str:
    if not username or not password:
        raise SystemExit("username and password are required")
    if len(password) < 12:
        raise SystemExit("refusing to seed: password must be at least 12 characters")
    maker = get_sessionmaker()
    with maker() as db:
        user = db.scalar(select(AdminUser).where(AdminUser.username == username))
        if user is None:
            user = AdminUser(username=username, password_hash=hash_password(password))
            db.add(user)
            action = "created"
        else:
            user.password_hash = hash_password(password)
            user.is_active = True
            action = "updated"
        db.commit()
        return f"admin user {username!r} {action}"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    settings = get_settings()
    if len(args) >= 2:
        username, password = args[0], args[1]
    else:
        username, password = settings.admin_seed_username, settings.admin_seed_password
    print(seed(username, password))
    return 0


if __name__ == "__main__":
    sys.exit(main())
