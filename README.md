# BrisLaw

BrisLaw adds Texas case law research to Claude Code. It uses CourtListener, the free case law database run by the Free Law Project. Once it's installed, you ask Claude in plain English and Claude runs BrisLaw for you.

This is a test version shared by Austin Brister. Please send him anything that breaks or reads wrong.

## What it does

- **Searches** Texas state and federal opinions, including the Texas Business Court. For example: "find Texas Supreme Court cases on the accommodation doctrine since 2015."
- **Pulls opinions** as clean text files. Older reported cases keep their S.W. page markers, so pin cites can be read from the file.
- **Finds citing cases** from CourtListener's full citation table. Each citing case shows how many times it cites the case, so the ones that actually discuss it rise to the top.
- **Cite-checks a draft** (Word, markdown, or text). It flags citations that point to a different case, wrong first pages, pins before the first page, wrong years, and short cites with no full cite. Only the citation strings leave your computer, never the draft.
- **Finds pin cites for unreported cases.** CourtListener has no Westlaw star pages. BrisLaw reads later opinions that cite the case and collects the "2024 WL 123456, at \*3" pins those courts used. It often recovers the WL number itself.
- **Warns about wrong records.** CourtListener sometimes stores a case's concurrence, its one-page judgment, or a replaced opinion as separate records with the same caption. BrisLaw says when you pulled one of those.

BrisLaw is not a citator. It does not tell you whether a case was followed or criticized. Check anything you will file on Lexis or Westlaw.

## Install (about 15 minutes, once)

You need Claude Code, either the Claude desktop app's Code tab or the command-line version. On Windows, Claude Code also needs Git for Windows (https://git-scm.com/downloads/win), which you may already have.

Commands below go in **Terminal** on a Mac, or **PowerShell** on Windows.

### 1. Accept the GitHub invitation

Austin invites you to the private BrisLaw repository on GitHub. Accept the emailed invitation; you need a free GitHub account.

### 2. Let your computer sign in to GitHub

Install the GitHub CLI from https://cli.github.com (Mac and Windows installers are on that page). Then run:

```
gh auth login
```

Choose **GitHub.com**, then **HTTPS**, answer **Yes** when it asks to authenticate Git with your GitHub credentials, and sign in with your web browser. This lets your computer download the private repository.

### 3. Install uv

uv installs BrisLaw's program and, if needed, the version of Python it runs on. You do not need to install Python yourself.

Mac:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows (PowerShell):

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Close the Terminal or PowerShell window and open a new one.

### 4. Install the BrisLaw program

```
uv tool install git+https://github.com/AustinBrister/BrisLaw
```

Check it worked:

```
brislaw --version
```

### 5. Connect your CourtListener account

Run:

```
brislaw auth login
```

It opens CourtListener's token page in your browser. Sign in, or create a free account there and confirm your email. Copy the API token on that page, then paste it into the Terminal or PowerShell window. The token is not shown as you paste it. BrisLaw checks the token with CourtListener and saves it in your Mac Keychain or Windows Credential Manager.

If you skip this step, Claude notices the first time you use BrisLaw and walks you through it. Each person uses their own token, with its own limit of 5,000 requests an hour.

Do not paste the token into a chat with Claude.

### 6. Add BrisLaw to Claude Code

In Claude Code, type these two commands one at a time:

```
/plugin marketplace add AustinBrister/BrisLaw
/plugin install brislaw@brislaw
```

If you have the command-line version of Claude Code, the same thing works from Terminal or PowerShell:

```
claude plugin marketplace add AustinBrister/BrisLaw
claude plugin install brislaw@brislaw
```

Then restart Claude Code.

### 7. Try it

Ask Claude things like:

- "Find Texas cases on whether produced water belongs to the mineral lessee."
- "Pull *Hooks v. Samson Lone Star*, 457 S.W.3d 52, and save it."
- "What cases cite 457 S.W.3d 52? Show me the ones that discuss it most."
- "Cite-check the brief in this folder."
- "I need a Westlaw pin cite for the unreported case No. 05-14-00741-CV."

You can also invoke it by name with `/brislaw:brislaw`.

## Updating

When Austin sends word of an update, run:

```
uv tool upgrade brislaw
```

Then update the Claude Code side. From Terminal or PowerShell:

```
claude plugin marketplace update brislaw
claude plugin update brislaw@brislaw
```

Or type `/plugin` in Claude Code and update BrisLaw from its menus. Then restart Claude Code. You can also turn on automatic updates for the BrisLaw marketplace under `/plugin`, in the Marketplaces tab.

## Settings

By default, saved opinions go to `Documents/BrisLaw/opinions` in your home folder. To use a different folder, or to have Claude save into your case folders, just tell Claude; it keeps the setting in `.brislaw/settings.json` in your home folder.

## What leaves your computer

BrisLaw sends your searches, case identifiers, and citation strings to CourtListener, and nothing else. It never uploads a draft you cite-check; only the citations in it go out. Opinions you pull are saved on your computer.

## If something goes wrong

- **`brislaw: command not found`**: close and reopen Terminal or PowerShell after step 3, then repeat step 4.
- **Authentication failed or not configured**: repeat step 5.
- **The plugin will not install, or the install step asks for a password**: repeat step 2, then try again.
- **Anything else**: send Austin the exact message you saw.
