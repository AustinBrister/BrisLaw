---
name: brislaw
description: Search and retrieve Texas case law from CourtListener (free). Trigger on "find cases on", "search case law", "pull/get [case name, citation, or docket number]", "what cites this", "is this still good law", "Shepardize", "check the progeny", "cite-check this draft", "check the cites", "what page is that on", "Westlaw pagination", "star page", "pin cite for an unreported case", or any CourtListener request. Searching for a phrase inside opinions without saving them can go to the official CourtListener connector if it is installed; this file says how.
---

# BrisLaw: Texas Case Law Search and Retrieval

Search and retrieve Texas case law from CourtListener directly within a Claude Code session. Everything through natural language, with no CLI flags exposed to the user.

This file covers the core workflows: search, get, and citing. The heavier workflows live in reference files that MUST be read before executing them:

| Workflow | Trigger examples | Read first |
|----------|-----------------|------------|
| Summarize | "summarize those", "assess relevance" | `references/summarize.md` |
| Synthesize | "research memo", "what's the law on" | `references/synthesize.md` |
| Save | "save to KB", "save to the case folder" | `references/save.md` |
| Settings | "/brislaw settings" | `references/settings.md` |
| Auth problems | auth errors, "Not configured" | `references/auth.md` |

Cite-checking a draft (`brislaw check`) and finding pin cites for cases without page breaks (`brislaw pins`) are covered below in this file.

All reference paths are relative to this skill's own folder, the base directory shown when the skill loads (inside the Claude Code plugin cache). Read them with the Read tool.

**Before the first command in a session,** confirm the `brislaw` program is installed by running `brislaw --version`. If the command is not found, stop and tell the user that BrisLaw's command-line program is not installed yet, and point them to the install steps in the BrisLaw README (https://github.com/AustinBrister/BrisLaw). If a command fails with an authentication error, read `references/auth.md`.

**Windows:** the commands in this file work the same in Git Bash and PowerShell. Put file paths that contain spaces in double quotes.

## What goes to the CourtListener connector

The official CourtListener connector, if the user has it installed in Claude, reads the same database as brislaw and draws on the same API quota (5,000 requests an hour). Its tool names end in `__search_document`, `__read_document`, `__call_endpoint`, and so on; load them through ToolSearch if they are deferred. brislaw stays the tool for searching, pulling, saving, citing, cite-checking, and pin cites, because it writes clean markdown straight to disk and reads drafts from disk. The connector returns everything into context and cannot save a file. Use the connector for these jobs, which brislaw does not do:

- **Searching inside opinions without saving them** (below).
- **Federal dockets and filings** for removed cases: docket search (`search` with type `r` or `d`), reading filings (`read_document` with a `recap_document_id`), docket alerts, and Pray and Pay requests.
- **CourtListener email alerts** for new opinions matching a search (`create_search_alert`). Ask the user before creating one.
- **Checking the API quota** (`get_api_usage`) after a rate-limit error.

Do not use the connector's `analyze_citations` for cite-checks. It needs the whole draft pasted into the tool call and does not check first pages; `brislaw check` reads the file, sends only the citation strings, and checks first pages.

**Searching inside opinions without saving them.** To find whether and where a phrase appears in one opinion or several, use `search_document`. Give it the `cluster_id` from brislaw's search JSON (the IDs are the same), or up to 10 `opinion_id`s in one call. It returns only the passages around each match, and `read_document` with `chunk_index` reads more around a match's position. Use it to screen a batch of search results, or to find the passage that matters before deciding what to pull. When the only question is whether and where the language appears, it replaces Analyze mode. Page markers come back as HTML spans (`class="star-pagination" label="60"`); the label is the same page data brislaw renders as `[*60]`. Once a case will be quoted or characterized in work product, pull and save it with brislaw as "When retrieval is required" says below, and quote and pin-cite from the saved copy.

## Invocation

This skill is invoked when the user says `/brislaw $ARGUMENTS` or asks to search CourtListener / search case law / find cases.

**The user's raw input is:** `$ARGUMENTS`

## Intent Parsing

Parse `$ARGUMENTS` to determine what the user wants. Before these rules, send phrase searches inside specific opinions ("does Hooks say X anywhere", "find where these cases discuss Y") to the CourtListener connector, per the section above. Otherwise apply these rules in order:

0. **Cite-check** ("cite-check this draft", "check the cites", "any bad citations in this"): Route to the **check** workflow. **Pin cites without page breaks** ("what page is that on", "I need the Westlaw pin", "star page for this unreported case"): Route to the **pins** workflow.
1. **Citation pattern** (volume + reporter + page, e.g., "718 S.W.3d 214", "457 SW3d 52") or **docket number** ("11-23-00222-CV", "23-0676", "PD-0123-24", "25-BC11B-0027", "19-50860"): Route to **get** workflow
2. **Result number** (bare integer like "3", "get 3"): Route to **get** workflow (references previous search)
3. **Explicit subcommand** ("search ...", "get ...", "citing ...", "save ...", "settings"): Route to the named workflow
4. **Retrieval phrases** ("get the Apollo case", "skim the Pryor case", "read it", "download it"): Route to **get** workflow
5. **Citing phrases** ("what cites this", "find cases citing X", "cases that cite", "Shepardize", "is this still good law", "check the citing cases", "progeny of"): Route to **citing** workflow
6. **Save phrases** ("save", "save to KB", "save this opinion", "save to knowledge base", "save to [matter]"): Route to **save** workflow (read `references/save.md`)
7. **Dig deeper** ("dig deeper", "more search angles", "search again"): Route to **search** workflow (user-driven new search with refined query)
8. **Settings** ("settings", "show settings", "my settings"): Route to **settings** command (read `references/settings.md`)
9. **Summarize** ("summarize", "assess", "relevance", "what do these cases say about", "summarize those", "summarize the top"): Route to **summarize** workflow (read `references/summarize.md`)
10. **Synthesize** ("synthesize", "research memo", "full research", "write up", "analyze the law on", "research the law on", "what's the law on"): Route to **synthesize** workflow (read `references/synthesize.md`)
11. **Everything else** (bare text like "estoppel cases in Texas"): Route to **search** workflow

## Natural Language to CLI Flag Translation

Before constructing the CLI command, extract these elements from the user's natural language:

**Court filters** (translate to `--court` flag):

| User says | CLI flag |
|-----------|----------|
| "Texas Supreme Court", "TXSC", "Texas high court" | `--court txsc` |
| "Court of Criminal Appeals", "CCA" | `--court cca` |
| "5th Circuit", "Fifth Circuit" | `--court 5thcir` |
| "Business Court" | `--court bizct` |
| "bankruptcy court", "Texas bankruptcy" | `--court bankruptcy` (S.D., N.D., W.D., and E.D. Tex.; not in the default) |
| "Texas state courts", "state courts" | `--court state` (includes the Business Court) |
| "federal courts", "Texas federal" | `--court federal` |
| One court of appeals ("Houston 14th", "Eastland") | `--court txctapp14`, `--court txctapp11` (recent opinions only; older ones are under `texapp`) |
| Several at once | `--court "txsc bizct"` (aliases and court IDs, space or comma separated) |
| "all courts", "any jurisdiction" | `--court all` |
| No jurisdiction mentioned | No `--court` flag (CLI defaults to the Texas Supreme Court, CCA, courts of appeals, Business Court, Fifth Circuit, and Texas federal district courts) |

**Non-Texas jurisdictions:** The `--court` flag only has built-in aliases for Texas courts. For other states (Oklahoma, New Mexico, Louisiana, etc.):

1. Use `--court all` to remove the Texas-only filter
2. Include the state or court name in the query string itself (e.g., `"Oklahoma produced water"`)
3. Inform the user: "Searching all jurisdictions since this involves [state] law."

Do NOT attempt `--court okla`, `--court nm`, or similar. The CLI warns on unrecognized aliases and they will not filter usefully.

**Search mode** (translate to `--mode` flag; the user never sees this flag):

| Query type | `--mode` | When to use |
|------------|----------|-------------|
| Specific terms of art, Boolean operators, fielded queries | `keyword` (default) | "Rule 11 agreement enforceability", `caseName:"Hooks"` |
| Conceptual/exploratory, no exact terms needed | `semantic` | "cases where court found bad faith in JOA operations" (server-side; can take 20-60s) |
| Broadening a search that returned sparse keyword results | `semantic` | Retry after keyword search found few relevant hits |

Mode selection rules:
- Default to `keyword` for targeted searches with known legal terminology
- Use `semantic` for exploratory/conceptual queries where the user describes an issue rather than naming a doctrine, and when broadening after sparse keyword results
- **Cross-check rule:** Never base final work product on semantic results alone. When a semantic search surfaces key cases, validate them with a targeted keyword search
- If a query contains fielded operators (`caseName:`, `court:`), Boolean operators (`AND`, `OR`, `NOT`), or proximity operators (`~5`), always use `keyword`. These only work in keyword mode

**Result count** (translate to `--limit` flag): "top 5" → `--limit 5`; "more results", "50 results" → `--limit 50`; default `--limit 10`, maximum 100. CourtListener returns 20 per page, so a limit over 20 costs one API call per 20.

**Sort order** (translate to `--sort`): "newest", "most recent" → `--sort newest`; "oldest", "earliest" → `--sort oldest`; "leading cases", "most cited" → `--sort cited`; default is relevance. `--min-cites N` keeps only cases cited at least N times, a quick way to find the leading case on an issue.

**Unpublished opinions:** CourtListener's search leaves out opinions marked unpublished unless asked (in a test, 214 unpublished Fifth Circuit and older Texas opinions on "royalty" were hidden). Add `--unpublished` when the user wants them or when a search for a known unpublished opinion comes back empty. In a test, every recent Eastland and El Paso court of appeals opinion on "produced water" was marked published, so CourtListener does not appear to track memorandum-opinion status for recent court of appeals opinions; do not rely on this flag to find or exclude them.

**Date ranges** (translate to the `--after` and `--before` flags; both bounds are inclusive):

| User says | CLI flags |
|-----------|-----------|
| "after 2020", "since 2020" | `--after 2020-01-01` |
| "before 2015" | `--before 2014-12-31` |
| "between 2018 and 2022" | `--after 2018-01-01 --before 2022-12-31` |
| "in 2019" | `--after 2019-01-01 --before 2019-12-31` |

The flags take `YYYY-MM-DD`. They also take a bare year, which `--after` reads as January 1 and `--before` reads as December 31 of that year, so `--before 2015` includes all of 2015. Write full dates to avoid that trap. The flags work in keyword and semantic mode and on `citing`.

Never put `filed_after:` or `filed_before:` inside the query string. CourtListener's v4 search API has no such operator and returns zero results. The CLI catches those tokens, moves them into the flags, and reports the filter in the JSON `data.filed_after` and `data.filed_before` fields, but write the flags directly. The native in-query form `dateFiled:[2015-01-01 TO *]` also works in keyword mode; prefer the flags.

In semantic mode, `data.total` ignores the date filter and reports about the same count as an unfiltered search. The results themselves are filtered.

**Example translations:**

User: "Texas Supreme Court cases about estoppel after 2020"
Command: `brislaw search "estoppel" --court txsc --after 2020-01-01 --limit 10 --json -q`

User: "cases where the court found the operator acted in bad faith under a JOA"
Command: `brislaw search "operator bad faith joint operating agreement" --mode semantic --limit 10 --json -q`

## CLI Invocation Rules

- ALL `search` and `citing` CLI invocations MUST include `--json -q` flags for machine-parseable, quiet output
- `get` commands use EITHER `--json -q` (triage mode) OR `-o FILE -q` (save mode) depending on intent. See Get Workflow below
- NEVER expose CLI flags or show the CLI command to the user. Everything goes through natural language
- Strip the natural language modifiers (court names, counts, dates) from the search query itself. They become flags, not part of the keyword query

### CRITICAL: Sequential Execution of `brislaw get`

**NEVER run multiple `brislaw get` commands in parallel.** Always run them sequentially. When multiple Bash tool calls run in parallel and any one fails (non-zero exit), Claude Code cancels ALL sibling parallel calls. A single bad citation, missing case, or network timeout kills the entire batch. This applies to ALL `brislaw get` invocations in every workflow, including inside sub-agents.

## Quote Discipline (MANDATORY in all analysis output)

Every report this skill produces (triage passages, summaries, synthesis memos) must make it impossible to mistake a paraphrase for a quotation. These rules also get passed verbatim to any analysis sub-agent (see the summarize/synthesize reference files):

1. **Quotation marks mean verbatim.** Text inside quotation marks or a blockquote must be copied word-for-word from opinion text you actually have in front of you, with a pin cite where the page is identifiable. If you cannot see the exact language, you cannot quote it.
2. **Everything else is paraphrase.** Statements of holdings, rules, or reasoning in your own words must NEVER appear inside quotation marks. Where a paraphrase could pass for the court's own formulation, tag it `(paraphrase)`.
3. **Legend up top.** Every summary or synthesis memo starts with one line: "Quoted material appears in quotation marks with citations; all other statements of holdings and rules are paraphrases."
4. **Never upgrade a paraphrase to a quote.** If a prior report, search snippet, or sub-agent output states a rule WITHOUT quotation marks, treat it as paraphrase no matter how much it sounds like court language. To quote it, first verify the exact wording against the opinion itself (Analyze mode: save with `-o`, search the file for the phrase with the Grep tool).
5. **Search snippets are not quotable.** Result snippets are processed excerpts, often truncated mid-sentence. Verify against the full opinion before quoting.

## Pin-Cite Discipline (MANDATORY in all analysis and drafting output)

Retrieved opinions carry reporter page markers as `[*N]` (start of reporter page N), and every saved opinion's metadata table includes a **Pagination** row stating whether markers are present. Text after `[*507]` sits on page 507 until the next marker; text before the first marker sits on the case's first page (from the citation).

1. **A pin cite may be written ONLY from a visible `[*N]` marker** in text actually in front of you. No marker, no pin.
2. **Never estimate a page** from opinion structure, length, or paragraph position. An estimated pin is indistinguishable from a verified one, which is exactly how bad pincites reach filings. Harvard-scan page labels are also not trustworthy on their own. In re Coppola's were off by two pages against the Supreme Court's own pin cites to it.
3. **When the Pagination row says NONE**, use one of these, in order of preference: (a) verify the pin on Lexis or Westlaw; (b) triangulate from citing opinions with `brislaw pins` (see the Pins Workflow), which collects how other courts pin-cite the target (Supreme Court self-citations are the gold standard; require agreement between at least two independent sources, and mark the pin `[PIN FROM CITING OPINION: verify on Westlaw]`); (c) cite without a pin and flag it inline: `[PIN UNVERIFIED: no pagination in free source]`. Never silently drop the flag.
4. **Expect NONE for post-~2018 cases.** CourtListener sources recent opinions from court-website slip opinions, and West reporter pagination is proprietary, so no free source will supply it. Plan on (a) or (b) for any recent case bound for a filing.
5. **Slip-op pins are for cases not yet in the reporter, and only those.** A case with no reporter citation yet (docket number only) is properly cited "slip op. at 12." Once a reporter citation exists, cite the reporter; never fall back to slip-op pins for a reporter-published case.

## Search Workflow

### Step 1: Construct and Run Command

```bash
brislaw search "QUERY" [--court COURT] [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--limit N] [--sort newest|oldest|cited] [--min-cites N] [--unpublished] --json -q
```

### Step 2: Parse JSON Response

ALWAYS check the `status` field first:

```json
{"status": "ok", "data": {"query": "...", "total": N, "results": [...]}}
```

If `status` is `"error"`, handle per Error Handling below. Otherwise extract `data.results`.

### Step 3: Clean Result Data

- Strip `<mark>` and `</mark>` tags from `case_name` and `snippet` fields
- Snippet fallback chain: `snippet_text` if present → `snippet` (tags stripped) → `opinions[0].snippet` (tags stripped)
- Read `opinion_types` and `note` on every result. CourtListener stores separately filed documents as separate records with the same caption: the Court's opinion, a concurrence, a dissent, the one-page judgment, a substituted opinion. When several appear in one result list, `note` says which results share a docket or a case name and filing date, and what each one is. Present the group as one case, name which record is the Court's opinion, and never offer a concurrence or dissent as the case without saying so.

### Step 4: Present Results

Always start with a conversational intro ("Found N Texas cases on [topic]...").

**Detailed format (1-3 results):** For each result: full formal citation, court, date, published status, cite count, and a 2-3 sentence relevance note written from the snippet.

**Compact format (4-10 results):** One line per result: short case name + court + year + one-line relevance note.

```
Found 19,475 results for "estoppel" across Texas courts. Showing top 10:

1. Pryor Legacy v. Ranches at Overhills, Tex. App. (2025): Estoppel defense in property dispute
2. Hensley v. SCJC, Tex. App. (2025): Estoppel plea granted then reversed
...
```

**Citation format:** `Case Name, Volume Reporter Page (Court Year)`. When the `citation` field is empty, fall back to court + docket number + date: `Tex. App., No. 04-24-00380-CV (Sept. 24, 2025)`. This mirrors how lawyers cite unpublished opinions.

**Always end with this escalation prompt** (every time, not conditional):

```
Retrieve any case with "get N" or by name. I can also:
- **Summarize** the top cases for relevance to your query
- **Save** any case to your knowledge base
- **Dig deeper** with additional search angles
```

## Get Workflow (Retrieval)

### Identifier Preference Order

1. **Cluster ID** (`brislaw get "cluster:2831451"`): most reliable. Every search result in `--json` output carries a `cluster_id`; pass it as `cluster:<id>`. **MANDATORY when running as a sub-agent or in any orchestrated/parallel workflow** (see Concurrent Sessions below). Strongly preferred everywhere else. If search JSON is in context, you already have the cluster_id.
2. **Result number** (`brislaw get 3`): shorthand when the user says "get 3" interactively. Reads `last_search.json`. Only safe when exactly one brislaw session is running and the reference is to the MOST RECENT search.
3. **Citation** (`brislaw get "718 S.W.3d 214"`): when no search JSON is in context (e.g., pulling cases from a brief). Fails for most cases decided after about 2018, because CourtListener lacks their S.W.3d cites; use the docket number for those. Takes the first match if a cite matches more than one record, and says so in a WARNING.
4. **Docket number** (`brislaw get "11-23-00222-CV"`, `"23-0676"`): the reliable identifier for recent and unreported cases. When the docket holds several records (opinion, judgment, concurrence, substituted opinion), brislaw picks the latest full-length record holding the Court's opinion and lists the others in the header.
5. **Case name** (`brislaw get "Apollo v Apache"`): last resort. Falls through to a `caseName:"..."` search and picks the first result in non-interactive mode, with a WARNING naming how many cases matched. NEVER trust without caption verification.

### Pre-Fetch Caption Check with `--preview`

When fetching by **citation or case name** (the ambiguous identifiers), check the caption cheaply BEFORE the full fetch:

```bash
brislaw get --preview "718 S.W.3d 214" -q
```

Preview returns metadata and the first portion of text only, a fraction of the tokens of a full fetch. Confirm the case name matches the intended case, then proceed with the full fetch. Cluster IDs are unambiguous and skip this step. Preview is a cheap pre-check, not a substitute for post-fetch Caption Verification below.

### Search Context Awareness

`brislaw get N` references the most recent search's `last_search.json`, overwritten by EVERY `brislaw search` call. Prefer `cluster:<cluster_id>` whenever the search JSON is in context. If using result numbers, only from the MOST RECENT search; for earlier searches, use `cluster:<cluster_id>` or the citation.

### Concurrent Sessions and Sub-Agents (CRITICAL)

`last_search.json` is a SINGLE GLOBAL FILE at `~/.cache/brislaw/last_search.json`, shared by every brislaw process on the machine. Any concurrent Claude session, sub-agent, or orchestrated research wave that runs `brislaw search` overwrites it. A `brislaw get N` issued after that overwrite silently fetches a case from SOMEONE ELSE'S search: the command succeeds, the JSON looks normal, and the wrong opinion gets saved under the intended case's filename. This exact failure has produced misfiled opinions in real research projects.

**Rule: in any sub-agent, orchestrated workflow, or session where parallel research agents may be running, NEVER use bare result numbers with `brislaw get` or `brislaw citing`. Always use `cluster:<cluster_id>` taken from your own search's JSON output (for `get`), or a full citation.** Caption verification is the backstop, not a substitute for this rule.

### Retrieval Modes

| Mode | CLI flags | What it does | Time |
|------|-----------|-------------|------|
| **Triage** | `--json -q` | Returns structured JSON with full opinion text in context | ~2-3s |
| **Analyze** | `-o ~/.brislaw/tmp/FILE.md -q` | Saves clean markdown to a working file to search and read | ~3-5s |
| **Save** | `-o DEST/FILE.md -q` | Saves clean markdown to permanent location | ~3-5s |
| **Save polished** | `-o DEST/FILE.md -q --footnotes` | Same as Save plus LLM footnote reformatting | ~15-20s |

**1. Triage** (default for first contact). User signals: "check this", "is it relevant?", "quick look", "skim it", or first interaction with a case. Run `brislaw get "cluster:ID" --json -q`, parse JSON (opinion text in `data.text`, may be raw HTML; read it directly), present a brief header plus 2-4 relevant passages with labels. The full opinion enters context, acceptable for triage; the local cache is populated so later Analyze/Save calls skip the API. A typical Texas appellate opinion is 10-20K tokens as raw HTML. In context-constrained settings prefer Analyze mode.

**2. Analyze** (Claude searches the opinion). User signals: "verify", "exact language", "pin cite", "be thorough", any request to confirm or deny specific language, or long opinions where only targeted passages are needed. Steps: `brislaw get IDENTIFIER -o ~/.brislaw/tmp/FILENAME.md -q` (the CLI creates the folder), search the file for the phrases with the Grep tool, Read the surrounding context, present findings with page references.

**3. Save** (permanent copy). User signals: "read it", "full opinion", "download", "grab it". Determine destination per Save Destination Resolution below, save with `-o` (the CLI creates the folder), verify caption, present header with file path. For "read it" signals do NOT dump opinion text inline; for "download" report path only.

**4. Save polished** (knowledge base / final work product). User signals: "save to KB", explicit `/brislaw save`. Same as Save plus `--footnotes` (LLM footnote reformatting, ~15s, polished `[^N]` markers). Read `references/save.md` for the full save workflow including duplicate handling.

**Mode inference:** triage by default on first contact; analyze when the request contains content-verification signals (analyze beats triage on first interaction then); save when the user wants to read it themselves; save polished for KB saves. User can always override.

### Filename Convention

Format: `{short-case-name}-{citation-slug}.md`

- **Short case name:** first party v second party, lowercase, hyphens, strip LLC/Inc/Corp suffixes ("Apollo Energy LLC v. Apache Corp." → `apollo-energy-v-apache`)
- **Citation slug:** volume + reporter (no dots) + page ("718 S.W.3d 214" → `718-sw3d-214`); no citation → docket number slug ("No. 04-24-00380-CV" → `04-24-00380-cv`)
- Normalization: lowercase, spaces → hyphens, strip punctuation, collapse repeated hyphens
- Examples: `apollo-energy-v-apache-718-sw3d-214.md`, `pryor-legacy-v-ranches-04-24-00380-cv.md`

### Caption Verification (MANDATORY before relying on any fetched opinion)

The filename is generated from the case you INTENDED to fetch. Nothing guarantees that is the case you ACTUALLY fetched. Citation and name lookups silently take the first match, and result numbers can be poisoned by concurrent sessions. Verify EVERY fetch in EVERY workflow (all four get modes, save, summarize pre-fetch, synthesize pre-fetch).

**For file saves (`-o FILE`):** every saved opinion starts with an H1 caption and metadata table. Immediately after EVERY save, read the whole header block:

Use the Read tool on the saved file with a limit of about 25 lines; the header block ends at the first `---` line.

**WARNING rows come first and must be acted on.** brislaw also prints each one to stdout as `WARNING: ...`, so they show up even with `-q`. They mean:

- *"This record holds only a concurring opinion ... The court's opinion is cluster:N"*: you saved a separate writing, not the case. Re-fetch with the named `cluster:N`. Caption matching cannot catch this, because both records carry the same caption and docket number.
- *"This record is 1 page long and is probably the judgment"*: re-fetch the opinion it names.
- *"A later opinion was filed on this docket"*: the court may have substituted a corrected opinion. Tell the user which one you saved and check which is current before quoting either.
- *"Citation ... matched N records"* or *"Case-name lookup ... matched N cases"*: the lookup guessed. Confirm the caption carefully.

The **Opinions** row names each opinion in the record and its author; the **Other records on this docket** row lists the rest. Compare the H1 line and Citation row (or Docket row, for cases without a reporter cite) against the intended case. Match loosely: party surnames may be reordered (appellant/appellee flip), names may include middle names or trust language, citation may be missing for unpublished opinions. Both principal parties matching = verified. Either party clearly belonging to a different case = MISMATCH.

**For triage fetches (`--json`):** before using `data.text`, check `data.case_name` and `data.citation` against the intended case with the same loose-match test, and read `data.warnings` and `data.related_records`.

**On mismatch:**

1. If a file was saved, immediately rename it: `mv DEST/FILENAME.md DEST/WRONG-CASE-DO-NOT-USE_FILENAME.md`
2. Tell the user (or report in sub-agent output): "Fetched [actual case] when attempting to retrieve [intended case]. Flagged the file and retrying."
3. Retry with a more precise identifier: run `brislaw search 'caseName:"Party1 v. Party2"' --json -q`, take the `cluster_id` of the matching result, then `brislaw get "cluster:<id>" -o ...`. If the case has an untried citation, try that.
4. Verify the retry the same way. If two attempts both mismatch, report the case as unretrieved rather than saving a wrong opinion. NEVER leave a mismatched opinion saved under the intended case's filename, and NEVER cite an opinion whose caption you did not verify.

### Save Destination Resolution

Used by all saving workflows. Read settings first (`references/settings.md`).

1. Read `settings.json` for `kb_path` and `case_folder_pattern` (expand `~`)
2. **If cwd is within `case_folder_pattern`** (working in a case folder): save to `research/opinions/` within the case folder, or `research/[topic]/opinions/` if a research topic was mentioned.
3. **If the user names a matter** but cwd is NOT in a case folder: locate the matter under `case_folder_pattern`, save to its `research/opinions/`
4. **Otherwise:** save to `kb_path` (default `~/Documents/BrisLaw/opinions`)

Files save flat within the destination (no further subdirectories). Dedup is automatic: same citation overwrites same file.

Note on confirmation: the Get workflow's Save mode saves without asking (the user just said "download it"; asking again is friction). The explicit Save workflow (`references/save.md`) DOES confirm the destination first, because KB saves are deliberate filing decisions. This asymmetry is intentional.

## Citing Workflow

Find cases that cite an opinion: good-law checks, progeny, doctrinal evolution.

brislaw reads CourtListener's citation table, the full citation graph. (CourtListener's `cites:` search operator reads the search index instead, which can miss most citing cases: it found 22 of the 121 opinions citing *Hooks v. Samson*.) The table records, for each citing opinion, how many times it cites the case. A case that cites it once is usually a string cite; a case that cites it a dozen times is discussing it. Citations to other records on the same docket (a separately filed concurrence, a substituted opinion) count as citations to the case.

### Step 1: Construct and Run

```bash
brislaw citing "IDENTIFIER" [--court COURT] [--after YYYY-MM-DD] [--before YYYY-MM-DD] [--sort newest|oldest|depth|cited] [--limit N] --json -q
```

Identifier resolution: citation ("457 SW3d 52") preferred; docket number ("23-0676") for cases without a reporter cite; `cluster:ID`; case name ("Hooks v Samson") works; result number ("citing 3") reads the same shared `last_search.json` as `get` and carries the same concurrent-session hazard. Use bare numbers ONLY in a single interactive session referencing the most recent search. In sub-agents or orchestrated workflows, always use the citation or `cluster:ID`. Default `--limit` is 20; `--limit 0` shows all.

Sort: `newest` (default) for "is this still good law"; `depth` to find the cases that discuss it most; `cited` for the most influential citing cases; `oldest` for doctrinal history. The default court filter is the Texas default; `--court all` includes every court.

Speed: about 10 seconds for a case cited 120 times. A case cited thousands of times reads 20 citation-table rows per API call; `--max-scan` (default 2000 rows) caps it and a warning says when the cap was hit.

### Step 2: Parse and Present

Same envelope as search (`status`, `data.results`), plus `data.citing_cases_all_courts` (before the court and date filters), `data.total` (after them), `data.warnings`, and `data.unresolved_opinion_ids`. Each result carries `depth` and a `note` ("Cites it 25 times." and, when only a concurrence or dissent cites it, "Only the dissenting opinion cites it."). Same adaptive formatting (compact for 4+, detailed for 1-3); include the depth.

**Cases with no reporter cite yet.** The citation table links citations mainly through reporter cites, so a recent case cited only by WL number or slip opinion shows few or no citing cases. For those, brislaw also searches for the party names after the decision date and returns the hits in `data.name_matches`, labeled as mentions to confirm. Present them separately from the citation-table results, and say they are unconfirmed.

**Caveats to communicate:**
- CourtListener's citation table is not a citator. It does not say whether a citing case followed, distinguished, or criticized the case. Read the high-depth cases to find out, and check Lexis or Westlaw for high-stakes reliance
- Zero citing results does NOT necessarily mean nothing cites the opinion, especially for recent cases

End with:

```
Retrieve any citing case with "get N" or by name. I can also:
- **Summarize** the citing cases for relevance
- **Save** any opinion to your knowledge base
- **Search** for the cited case itself to review its holding
```

### Citing in Research Workflows

In summarize/synthesize, use citing as validation: after identifying key authorities, run `brislaw citing --sort depth` on the most important ones; read the top few to check whether holdings have been distinguished, limited, or overruled; treat the `depth` field (times the citing opinion cites the case) as a centrality signal.

## Check Workflow (Cite-Checking a Draft)

When the user asks to cite-check a draft, check its cites, or look for bad or made-up citations:

```bash
brislaw check "DRAFT.docx" -o "cite-check-DRAFTNAME.md"
```

It reads Word (.docx), markdown, or text files, including Word footnotes; for a PDF, ask the user for the Word version. Save the report next to the draft unless the user names another place. It finds every case citation with eyecite, sends only the citation strings (not the draft) to CourtListener at 60 citations a minute, and writes a report with problems first. stdout gets a one-line summary. For Word files, the report's line numbers are paragraph numbers, counting the body first and then the footnotes.

The report's sections, and what to do with each:

- **Problems.** A case name that shares no distinctive word with the case at that cite means the cite points to a different case; this is how made-up citations show up. A wrong first page means the cited page falls inside another case (in testing, "Smith v. Jones Oil Co., 512 S.W.3d 901" was flagged both ways: page 901 is inside *ExxonMobil Pipeline v. Coleman*, which starts at 895). A pin before the first page, or a cite CourtListener has at a different volume and page, is also a problem. Report every problem to the user.
- **Not in CourtListener.** Either CourtListener has the case but not the cite (common for cases after about 2018; the report names the case, court, date, and docket number) or it found no case at all by that name. The second kind is the one to worry about. Check both kinds on Lexis before calling a cite wrong.
- **Short cites.** A short cite ("600 S.W.3d at 12") with no full citation of that volume in the draft, or a pin before the case's first page.
- **Minor notes.** A year that differs from CourtListener's date, or more than one case at the cite. Usually harmless; mention them.
- **Checked out.** CourtListener has a case that starts on the cited page and shares a distinctive word with the draft's case name. That is all it means: it does not check pins beyond the first page, quotations, parentheticals, or subsequent history.

Use `--json` for machine-readable output (`data.citations`, `data.counts`).

## Pins Workflow (Pin Cites Without Page Breaks)

CourtListener has no Westlaw star pages ("2024 WL 123456, at \*3"); only Westlaw has them, and no formula converts a slip-opinion page to one. It also lacks S.W.3d page breaks for most cases decided after about 2018. When the user needs a pin for such a case, especially an unreported one:

```bash
brislaw pins "DOCKET-NUMBER or CITATION" -o "pins-SHORTNAME.md"
```

It reads the opinions that discuss the case most (from the citation table) and, for cases cited by WL number, later opinions that mention its docket number or party names. It collects every "S.W.3d at N" and "WL ___, at \*N" pin those courts used, grouped by page, with the text around each. It also recovers the case's WL number from citing opinions when CourtListener doesn't list it. In testing it found the WL number for three unreported courts of appeals cases that CourtListener listed with no cite at all. `--sources N` (default 25) reads more opinions.

How to use the result:

- The text around each pin is the citing court's words, not the case's. It shows which page another court used for a point. Match the user's proposition to a pin only when the citing court is plainly citing the same passage, and say that is the basis.
- A pin found this way is secondhand. Mark it `[PIN FROM CITING OPINION: verify on Westlaw]` in any draft, per the Pin-Cite Discipline section.
- Read any "Caution" line. If the WL cite's year differs from the record's date, the citing courts may be citing a different opinion in the same case.
- If the report says CourtListener's own copy has page markers, pin from the saved opinion instead.

## Error Handling

ALWAYS check the `status` field before accessing data.

| Error code | User message |
|------------|-------------|
| `auth_error` | "CourtListener API token not configured. Run `brislaw auth login` in your terminal." (Then see `references/auth.md`.) |
| `rate_limit` | "CourtListener rate limit reached. Please wait a few minutes." |
| `not_found` | "No cases found for that citation. Check the volume, reporter, and page." |
| `connection_error` | "Cannot connect to CourtListener. Check your internet connection." |
| `timeout` | "CourtListener timed out after three automatic retries. Try once more; if semantic mode keeps timing out, rerun in keyword mode." |
| `invalid_date` | A date flag was malformed, or the start date falls after the end date. Fix the flag and rerun; do not report this to the user as an empty search. |
| `invalid_sort` | A `--sort` value was wrong. Fix it and rerun. |
| `unsupported_file` | `brislaw check` was given a PDF or other file type. Ask the user for the Word version. |
| Status "ok" but empty results | "No cases matched that search. Try different terms or broaden the jurisdiction." |

If the CLI exits non-zero without valid JSON: "The brislaw CLI encountered an error. Check that it's installed (`which brislaw`) and authenticated (`brislaw auth status`)." For auth failures, read `references/auth.md`.
