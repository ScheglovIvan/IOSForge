"""Password hashing (argon2) for admin operators (SPEC §7 / §8 security).

Plaintext passwords never touch the DB, logs, or the repo — only argon2 hashes
are stored. Verification is constant-time via the argon2 library.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_password(plaintext: str) -> str:
    """Return an argon2 hash for ``plaintext`` (suitable for DB storage)."""
    return _ph.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Constant-time verify of ``plaintext`` against a stored argon2 hash."""
    try:
        return _ph.verify(stored_hash, plaintext)
    except VerifyMismatchError:
        return False
    except Exception:  # malformed hash etc. — never leak details, just fail closed
        return False
