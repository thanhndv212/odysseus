"""
Email active context helpers — track which email the user is currently viewing.
Cleared between requests by chat_routes.
"""

from typing import Dict, Optional

_active_email_ref: Optional[Dict[str, str]] = None


def set_active_email(uid: Optional[str], folder: Optional[str] = None, account: Optional[str] = None,
                     subject: Optional[str] = None, sender: Optional[str] = None) -> None:
    """Stash the email currently open in the UI. None clears it."""
    global _active_email_ref
    if not uid:
        _active_email_ref = None
        return
    _active_email_ref = {
        "uid": str(uid),
        "folder": str(folder or "INBOX"),
        "account": str(account or ""),
        "subject": str(subject or ""),
        "from": str(sender or ""),
    }


def get_active_email() -> Optional[Dict[str, str]]:
    return _active_email_ref


def clear_active_email() -> None:
    global _active_email_ref
    _active_email_ref = None
