# BrisLaw Auth Troubleshooting

BrisLaw needs a free CourtListener API token. Each person uses their own token, which carries its own limit of 5,000 requests an hour.

## Checking the token

Run `brislaw auth status`. It reports whether a token is stored and shows its first few characters.

## Storing or replacing the token

The user stores the token themselves, in their own terminal:

1. Sign in to CourtListener (a free account) and copy the API token from https://www.courtlistener.com/profile/api-token/.
2. In a terminal window (Terminal on a Mac, PowerShell on Windows), run `brislaw auth login` and paste the token when asked. The token is not shown as it is pasted.

The token goes into the Mac Keychain or the Windows Credential Manager under the service name `brislaw`.

**Never ask the user to paste the token into the chat, and never type or store a token for them.** If the user pastes one into the chat anyway, tell them to run `brislaw auth login` themselves and to consider generating a new token on CourtListener.

## When the keychain does not work

On a machine where the system keychain is unavailable, BrisLaw also reads the token from the `COURTLISTENER_API_TOKEN` environment variable. The user sets it themselves.

## When auth fails during a session

1. Run `brislaw auth status` to confirm a token is stored.
2. If it says the token is not configured, ask the user to run `brislaw auth login` in their own terminal.
3. If a stored token is rejected (an authentication error from CourtListener), the token may have been replaced on CourtListener's site. Ask the user to copy the current token and run `brislaw auth login` again.
