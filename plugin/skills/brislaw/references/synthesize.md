# BrisLaw Synthesize Workflow

Doctrinal synthesis memo via multi-query research and an analysis sub-agent. Read `references/settings.md` and load settings before starting.

## Step 1: Determine Research Strategy (Context-Driven)

- **Open-ended request** ("research produced water law"): propose a research plan with 3-5 search angles. "I'd suggest searching from these angles: 1) [angle], 2) [angle], 3) [angle]. Shall I proceed, or would you adjust?" Wait for approval or modification.
- **Direct request** ("grab the Cactus Water case and its progeny"): go autonomous, no planning overhead.
- **Direction uncertain or sparse results:** check in. "I found N cases on this angle. Should I try a broader query or is this sufficient?"

## Step 2: Execute Multi-Query Search

Run one search per angle, mixing modes as appropriate:

1. **First pass per angle: semantic** -- cast a wide net
   ```bash
   brislaw search "QUERY" --mode semantic --json -q
   ```
2. **Targeted follow-ups: keyword** -- specific doctrines, statutory provisions, terms of art identified in the first pass
   ```bash
   brislaw search "QUERY" --json -q
   ```
3. **Citing check on key authorities** -- after identifying the most important cases. `--sort depth` puts the cases that discuss the authority most at the top; those are the ones worth reading for how courts have applied it.
   ```bash
   brislaw citing "CITATION" --sort depth --json -q
   ```

- **Deduplicate** results across searches by `cluster_id`
- **Cross-check rule:** a case that surfaces only in semantic results (not keyword) gets flagged for closer review -- it may be tangential
- **Select top cases** for detailed reading (Claude's discretion, typically 3-8 depending on scope)

## Step 3: Build the Case List (do NOT pre-fetch opinions)

For each selected case, collect from the search JSON: `case_name`, `citation`, `court`, `date_filed`, `judges` (if present), and `cluster_id`. The synthesis sub-agent fetches the opinions itself (Step 5) -- keeping 10-20K tokens per opinion out of the main context is the point of delegating.

**Fallback:** if the sub-agent environment cannot run Bash (rare), pre-fetch each opinion in the main context with `brislaw get "cluster:ID" --json -q` (SEQUENTIALLY, never in parallel), verify each caption, and pass the texts in the prompt instead.

## Step 4: Gather Case Context (If Needed)

If the research question requires factual grounding (mentions "our case", "the lease in this matter", specific party names), OFFER to gather case context. Offered, never forced.

**Inline context** (always available): the user describes facts in conversation. Capture party names and roles, key factual issues, relevant contract/deed provisions, procedural posture.

**Folder-aware context** (only if `case_folder_pattern` is set in settings.json): scan a case folder lightweight-style:

1. List top-level directory contents of the specified folder
2. Look for obviously relevant files by name: memos, summaries, overviews, engagement letters (.docx, .md, .pdf)
3. Read at most 3 files (prefer short memos over lengthy pleadings)
4. Extract: party names, key issues, relevant provisions, procedural posture
5. NEVER traverse more than 1 level deep. NEVER build an index. NEVER read every file.

The sub-agent uses context to ground analysis in the matter's facts, assess relevance of holdings, identify analogous or distinguishable fact patterns, and tailor the synthesis to the user's position. (The summarize workflow can use this same context-gathering when factual grounding would help.)

## Step 5: Spawn the Synthesis Sub-Agent

Spawn a sub-agent via the **Agent tool** (`subagent_type: "general-purpose"`). Do NOT pin a model -- inherit the session model.

The sub-agent prompt must include:

1. **Persona** from settings.json
2. **User's research question**
3. **Case context** (if gathered in Step 4)
4. **The case list** from Step 3 (metadata + cluster IDs)
5. **Fetch instructions:**
   - For each case, run `brislaw get "cluster:CLUSTER_ID" --json -q`
   - SEQUENTIALLY -- never multiple `brislaw get` calls in parallel
   - Opinion text is in `data.text` (may be raw HTML -- read it directly)
   - Before using each opinion, verify `data.case_name` against the intended case (loose match on principal party names). On mismatch, skip and report -- never synthesize from an unverified opinion
   - NEVER use bare result numbers -- cluster IDs only
   - If a cluster-ID fetch fails, retry once with the citation string; if that fails, skip and note it in the memo
6. **Quote Discipline rules from SKILL.md, passed VERBATIM.** Quotation marks mean verbatim opinion text with a pin cite; every rule statement in the memo's own words stays outside quotation marks (tag `(paraphrase)` where confusable); the memo opens with the legend line; search snippets are never quotable.
7. **Output format guidance** -- Claude's discretion based on context:
   - *Narrative analysis* (default): present the law, analyze holdings, assess trajectory
   - *IRAC memo*: when the user asks a specific legal question
   - *Annotated outline*: when mapping an area of law
   - User can override: "/brislaw synthesize ... as IRAC memo"
8. **Citation guidance:** pin cites when the specific page is identifiable from the opinion text; general citation otherwise

## Step 6: Present Results and Ask About Destination

Present the synthesis memo inline, preserving its quote/paraphrase markings exactly. Then ask:

"Would you like me to save this research memo? I can save to:
1. Your knowledge base at `[kb_path from settings]`
2. `[Matter folder]/research/` (if case context is active)
3. A custom location"

If yes, use the Save Workflow's Research Memo Save path (`references/save.md`) with filename `research-memo-{topic-slug}-{date}.md`.

## Anti-Patterns

- NEVER generate the synthesis inline in the main context. Always delegate to the sub-agent.
- NEVER pass bare result numbers to the sub-agent. Cluster IDs only.
- NEVER do deep case folder indexing. Lightweight scan only (Step 4 limits).
- NEVER save without asking the user.
- NEVER hardcode paths. All paths from settings.json.
- NEVER promote a paraphrase from the memo into a quotation when relaying or reusing it downstream.
