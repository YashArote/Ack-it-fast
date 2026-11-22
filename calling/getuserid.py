from __future__ import annotations

TARGET_USER_OBJECT_ID = "6e2d0319-5751-4832-9281-98ed20b705ba"


def get_user_object_id(email: str) -> str:
    """Return the fixed temporary target ID for an assigned user's email."""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("An assigned user's email is required to place a call")
    return TARGET_USER_OBJECT_ID
