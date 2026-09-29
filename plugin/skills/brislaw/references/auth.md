# BrisLaw Auth Troubleshooting

BrisLaw needs a free CourtListener API token. Each person uses their own token, which carries its own limit of 5,000 requests an hour.

## Checking the token

Run `brislaw auth status --json`. `data.configured` says whether a token is stored, and `data.source` says where it came from.

## Storing or replacing the token

The user stores the token themselves, in their own terminal, with one command:

```
brislaw auth login
```

It walks them through the rest:

1. It opens CourtListener's token page (https://www.courtlistener.com/profile/api-token/) in their browser. A user who is not signed in is sent to sign in first; a user without an account can create a free one there, and must confirm the email before the token page works.
2. The user copies the token from that page.
3. The user pastes it into the terminal. It is not shown as it is pasted.
4. BrisLaw checks the token with CourtListener before saving it. A rejected token is not saved, and the command says to copy it again.

The token goes into the Mac Keychain or the Windows Credential Manager under the service name `brislaw`.

If the browser does not open (for example, on a remote machine), `brislaw auth login --no-browser` prints the page address instead.

**How to start it for the user.** If you can start a command in the user's own terminal (for example, the terminal panel in the Claude desktop app), start `brislaw auth login` there and tell the user to paste the token into that terminal. If not, ask the user to open Terminal (Mac) or PowerShell (Windows) and run the command. After they finish, confirm with `brislaw auth status --json`.

**Never ask the user to paste the token into the chat, and never type or store a token for them.** If the user pastes one into the chat anyway, tell them to run `brislaw auth login` themselves and to consider generating a new token on CourtListener.

## When the keychain does not work

On a machine where the system keychain is unavailable, BrisLaw also reads the token from the `COURTLISTENER_API_TOKEN` environment variable. The user sets it themselves.

## When auth fails during a session

1. Run `brislaw auth status` to confirm a token is stored.
2. If it says the token is not configured, ask the user to run `brislaw auth login` in their own terminal.
3. If a stored token is rejected (an authentication error from CourtListener), the token may have been replaced on CourtListener's site. Ask the user to copy the current token and run `brislaw auth login` again.
