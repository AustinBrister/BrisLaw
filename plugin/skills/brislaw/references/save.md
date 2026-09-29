# BrisLaw Save Workflow

Read `references/settings.md` and load settings before starting. This workflow handles two save formats:

- **Raw opinion save** -- saving a fetched opinion as markdown
- **Research memo save** -- saving analysis/synthesis output (used after the synthesize workflow)

Unlike the Get workflow's quick Save mode (which saves without asking when the user says "download it"), this explicit Save workflow ALWAYS confirms the destination first -- KB saves are deliberate filing decisions.

## Raw Opinion Save

### Step 1: Determine Destination

Use the **Save Destination Resolution** logic from SKILL.md:
1. If cwd is within `case_folder_pattern` → `research/opinions/` (or `research/[topic]/opinions/`) within the case folder
2. If user mentions a specific matter → that matter's `research/opinions/` under `case_folder_pattern`
3. Otherwise → `kb_path` from settings.json (default: `~/Documents/BrisLaw/opinions`)

The CLI creates the destination folder if it does not exist.

**ALWAYS ask the user to confirm the destination before saving.** Example: "I'll save this to `[destination]`. Sound good?"

### Step 2: Generate Filename

Use the Filename Convention from SKILL.md: `{short-case-name}-{citation-slug}.md`, same normalization rules.

### Step 3: Check for Duplicates

Check whether a file with the same name already exists at the destination.

**If it exists:**

1. Read the existing file (at minimum the H1 caption and metadata table; skim further if needed)
2. Compare directly -- no sub-agent needed for this. Determine: same opinion (same citation)? Which source ("Source: CourtListener" vs. LEXIS headers)? Which appears more complete (length, footnotes, metadata)?
3. Present the comparison: "This opinion already exists at `[path]`: [2-3 sentence comparison]. Would you like to: **replace** the existing file, **keep both** (I'll add a suffix like `-courtlistener`), or **skip** this save?"
4. Act on the user's choice

**If it does not exist:** proceed directly.

### Step 4: Save via CLI

```bash
brislaw get "cluster:CLUSTER_ID" -o DESTINATION/FILENAME.md -q --footnotes
```

Prefer `cluster:<cluster_id>` over a citation string when search JSON is in context. Use `--footnotes` for knowledge base saves (LLM footnote reformatting, ~15s, polished output). The CLI produces a complete markdown document with metadata table, footnotes, and citation links.

### Step 5: Verify the Caption

Run the **Caption Verification** check from SKILL.md (read the header block, the lines before the first `---`, with the Read tool; loose-match the H1 and Citation row; and act on any WARNING row). On mismatch: rename with the `WRONG-CASE-DO-NOT-USE_` prefix, tell the user, retry with a `caseName:"..."` search + `cluster:<id>` fetch.

### Step 6: Confirm

"Saved to `[full path]`."

## Research Memo Save

Used after the synthesize workflow produces analysis output.

1. **Determine destination:** same logic as raw opinion save Step 1 (read settings, infer kb_path or matter folder, ask user to confirm)
2. **Generate filename:** `research-memo-{topic-slug}-{date}.md`
   - `topic-slug`: slugified research topic (lowercase, hyphens, max 40 chars)
   - `date`: current date YYYY-MM-DD
   - Example: `research-memo-produced-water-ownership-2026-02-21.md`
3. **Check for duplicates:** same logic as Step 3 above
4. **Save via Write tool** (not the CLI -- this is generated analysis, not a fetched opinion)
5. **Confirm:** "Saved research memo to `[full path]`."
