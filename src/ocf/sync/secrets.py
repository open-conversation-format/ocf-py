"""Meilisearch API key in the system credential store.

Windows Credential Manager, macOS Keychain or the Secret Service on
Linux, via ``keyring``. The key is stored per Meilisearch URL and never
written into the settings file.
"""

from __future__ import annotations

SERVICE = "ocf-sync"


def _account(url: str) -> str:
    return url.strip().rstrip("/")


def available() -> bool:
    try:
        import keyring  # noqa: F401
    except ImportError:
        return False
    return True


def get_key(url: str | None) -> str | None:
    if not url:
        return None
    try:
        import keyring

        return keyring.get_password(SERVICE, _account(url)) or None
    except Exception:  # noqa: BLE001 - missing package or backend
        return None


def set_key(url: str, key: str) -> None:
    """Store ``key`` for ``url``; an empty key removes the stored one."""
    import keyring

    if key:
        keyring.set_password(SERVICE, _account(url), key)
    else:
        delete_key(url)


def delete_key(url: str) -> None:
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE, _account(url))
        except PasswordDeleteError:
            pass
    except ImportError:
        pass


__all__ = ["SERVICE", "available", "delete_key", "get_key", "set_key"]
