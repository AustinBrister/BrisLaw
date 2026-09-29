# BrisLaw Summarize Workflow

Per-case analysis of search results. Read `references/settings.md` and load settings before starting.

Output has four required components for every case: holding and reasoning, key quotes, procedural context, and relevance assessment.

## Step 1: Determine Cases to Summarize

- **Triggered after a search** ("summarize those", "summarize the top"): use the previously displayed search results
- **Standalone request** ("/brislaw summarize royalty dispute cases"): run `brislaw search "QUERY" --json -q` first

**Number of cases** -- Claude's discretion based on query specificity and result quality:
- Specific query (e.g., "estoppel in property disputes"): 3-5 most relevant
- Broad survey (e.g., "recent produced water cases"): 5-8 most relevant
- Targeted request ("summarize the Cactus Water case"): 1
- Consider total result count, snippet relevance signals, user's apparent intent

## Step 2: Build the Case List (do NOT pre-fetch opinions)

From the search JSON, build a case list for the sub-agent. For each selected case include: `case_name`, `citation`, `court`, `date_filed`, `judges` (if present), and most importantly `cluster_id`.

Do NOT fetch the opinion texts into the main context. The analysis sub-agent fetches them itself by cluster ID (Step 4). This keeps 10-20K tokens per opinion out of the main conversation. The local SQLite cache means any case already triaged in this session costs the sub-agent nothing extra to fetch.

**Fallback:** if the sub-agent environment cannot run Bash (rare), pre-fetch each opinion in the main context with `brislaw get "cluster:ID" --json -q` (SEQUENTIALLY, never in parallel), verify each caption, and pass the texts in the prompt instead.

## Step 3: Read Settings for Persona

Read `~/.brislaw/settings.json` via the Read tool. Extract `persona`. If missing, use the default persona from `references/settings.md`.

## Step 4: Spawn the Analysis Sub-Agent

Spawn a sub-agent via the **Agent tool** (`subagent_type: "general-purpose"`). Do NOT pin a model -- inherit the session model.

The sub-agent prompt must include:

1. **Persona** from settings.json
2. **User's original query/research question**
3. **The case list** from Step 2 (metadata + cluster IDs)
4. **Fetch instructions:**
   - For each case, run `brislaw get "cluster:CLUSTER_ID" --json -q`
   - SEQUENTIALLY -- never multiple `brislaw get` calls in parallel
   - Opinion text is in `data.text` (may be raw HTML -- read it directly)
   - Before using each opinion, verify `data.case_name` against the intended case (loose match on principal party names). On mismatch, skip the case and report the mismatch -- do not analyze the wrong opinion
   - NEVER use bare result numbers (`brislaw get 3`) -- cluster IDs only
   - If a cluster-ID fetch fails, retry once with the citation string; if that fails, skip and note it
5. **Analysis instructions:** "For each case, produce analysis with these four components:
   - *Holding and reasoning* -- what did the court decide and why
   - *Key quotes* -- the most important passages from the opinion with page references if available
   - *Procedural context* -- how did this case get here
   - *Relevance assessment* -- how relevant is this case to the user's query and why"
6. **Quote Discipline rules from SKILL.md, passed VERBATIM.** In particular: quotation marks mean verbatim text copied from the opinion in front of you with a cite; all rule statements in the sub-agent's own words stay outside quotation marks (tag `(paraphrase)` where confusable); the report starts with the legend line; never quote from search snippets.
7. **Structure guidance** -- Claude's discretion:
   - Case-by-case ranked by relevance for straightforward queries (default)
   - Themed groupings when exploring an area of law
   - Chronological when doctrinal evolution matters (e.g., TXSC ruling then downstream applications)
8. **Length guidance** -- Claude's discretion based on case complexity and relevance

## Step 5: Present Results and Ask About Destination

Present the sub-agent's analysis directly to the user (inline), preserving its quote/paraphrase markings exactly -- do not add or remove quotation marks when relaying.

Then offer:

"Would you like me to:
1. **Save** this analysis to a file (knowledge base or case folder)
2. **Both** -- you already have it inline, and I can also save a copy
3. **Dig deeper** into any of these cases, or run a full synthesis"

## Anti-Patterns

- NEVER hardcode the persona prompt. Always read from settings.json (with fallback to default).
- NEVER limit to a fixed number of cases. Assess based on context.
- NEVER pass bare result numbers to the sub-agent. Cluster IDs only.
- NEVER promote a paraphrase from the sub-agent's report into a quotation when relaying or reusing it downstream.
