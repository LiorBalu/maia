"""Password policy of dashboard users: history, reuse check, update."""

from datetime import datetime

from fastapi import HTTPException, status
from werkzeug.security import check_password_hash, generate_password_hash

from maia.models.users import PasswordHistoryEntry, UserDocument


def _is_reused_password(new_password: str, user: UserDocument) -> bool:
    """True when the new password equals the current one or one of the stored previous hashes."""
    if user.password and check_password_hash(user.password, new_password):
        return True

    for entry in user.password_history or []:
        if entry.hash and check_password_hash(entry.hash, new_password):
            return True

    return False


def update_user_password(user: UserDocument, new_password: str) -> None:
    """Set a new password: refuse empty or reused ones, keep the last 3 hashes in the history, stamp the change date.
    """
    if not new_password or not new_password.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password cannot be empty")

    if _is_reused_password(new_password, user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="New password cannot be same as current or recent passwords"
        )

    new_hash = generate_password_hash(new_password)

    history = user.password_history or []

    if user.password:
        history.insert(0, PasswordHistoryEntry(hash=user.password))

    user.password_history = history[:3]  # Keep last 3 old passwords

    user.password = new_hash
    user.password_last_changed_at = datetime.utcnow()

    user.save()
