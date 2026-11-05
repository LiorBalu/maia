"""Password policy of dashboard users: history, reuse check, update."""

from datetime import datetime

from fastapi import HTTPException, status
from werkzeug.security import check_password_hash, generate_password_hash

from maia.models.users import PasswordHistoryEntry, UserDocument


def _is_current_or_recent_password(candidate_password: str, user_doc: UserDocument) -> bool:
    """True when the new password equals the current one or one of the stored previous hashes."""
    if user_doc.password and check_password_hash(user_doc.password, candidate_password):
        return True

    for history_entry in user_doc.password_history or []:
        if history_entry.hash and check_password_hash(history_entry.hash, candidate_password):
            return True

    return False


def _verify_password_is_acceptable(candidate_password: str, user_doc: UserDocument) -> None:
    """Raise HTTP 400 when the new password is empty or equals the current or a recent one."""
    if not candidate_password or not candidate_password.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Password cannot be empty")

    if _is_current_or_recent_password(candidate_password, user_doc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="New password cannot be same as current or recent passwords"
        )


def _replace_password_hash(user_doc: UserDocument, new_password_hash: str) -> None:
    """Push the current hash into the password history (the last 3 are kept), then set the new hash."""
    history_entries = user_doc.password_history or []

    if user_doc.password:
        history_entries.insert(0, PasswordHistoryEntry(hash=user_doc.password))

    user_doc.password_history = history_entries[:3]  # Keep last 3 old passwords

    user_doc.password = new_password_hash


def rotate_user_password(user_doc: UserDocument, candidate_password: str) -> None:
    """Set a new password: refuse empty or reused ones, keep the last 3 hashes in the history, stamp the change date.
    """
    _verify_password_is_acceptable(candidate_password, user_doc)

    new_password_hash = generate_password_hash(candidate_password)

    _replace_password_hash(user_doc, new_password_hash)
    user_doc.password_last_changed_at = datetime.utcnow()

    user_doc.save()
