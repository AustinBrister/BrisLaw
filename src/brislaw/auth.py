"""CourtListener API token storage: the macOS Keychain or Windows Credential Manager.

The token can also come from the COURTLISTENER_API_TOKEN environment
variable, for machines where the system keyring is unavailable.
"""

from __future__ import annotations

import os

import keyring
import keyring.errors

SERVICE = "brislaw"
ACCOUNT = "courtlistener-api-token"


def get_api_token() -> str | None:
    """Retrieve the CourtListener API token.

    Checks the system keyring under the primary service name, then the old
    "courtlistener" service name, then the COURTLISTENER_API_TOKEN
    environment variable.

    Returns:
        The API token string, or None if not configured.
    """
    for service in (SERVICE, "courtlistener"):
        try:
            token = keyring.get_password(service, ACCOUNT)
        except keyring.errors.KeyringError:
            token = None
        if token:
            return token
    return os.environ.get("COURTLISTENER_API_TOKEN") or None


def set_api_token(token: str) -> None:
    """Store the CourtListener API token in the system keyring.

    Args:
        token: The API token to store.
    """
    keyring.set_password(SERVICE, ACCOUNT, token)


def delete_api_token() -> None:
    """Remove the CourtListener API token from the system keyring.

    Silently succeeds if no token is stored.
    """
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        pass
