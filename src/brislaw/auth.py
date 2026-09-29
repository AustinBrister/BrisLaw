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


# Where a user copies their token. Signed-out visitors are sent to sign in
# (or create a free account) first.
TOKEN_PAGE = "https://www.courtlistener.com/profile/api-token/"


def find_api_token() -> tuple[str | None, str | None]:
    """Return (token, where it came from): "keychain", "environment", or None.

    Checks the system keyring under the primary service name, then the old
    "courtlistener" service name, then the COURTLISTENER_API_TOKEN
    environment variable.
    """
    for service in (SERVICE, "courtlistener"):
        try:
            token = keyring.get_password(service, ACCOUNT)
        except keyring.errors.KeyringError:
            token = None
        if token:
            return token, "keychain"
    token = os.environ.get("COURTLISTENER_API_TOKEN") or None
    return token, ("environment" if token else None)


def get_api_token() -> str | None:
    """Retrieve the CourtListener API token, or None if not configured."""
    return find_api_token()[0]


def check_api_token(token: str) -> bool | None:
    """Ask CourtListener whether a token is valid.

    Returns True if accepted, False if rejected, None if CourtListener could
    not be reached (so the caller can store the token anyway).
    """
    import httpx

    try:
        resp = httpx.get(
            "https://www.courtlistener.com/api/rest/v4/courts/tex/",
            params={"fields": "id"},
            headers={"Authorization": f"Token {token}"},
            timeout=20,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code in (401, 403):
        return False
    return resp.status_code < 500 or None


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
