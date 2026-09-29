# BrisLaw Settings

Read this before processing any summarize, synthesize, or save request.

## Settings Load Procedure

1. **Read settings file:** Use the Read tool to read `~/.brislaw/settings.json` (on Windows, `C:\Users\<name>\.brislaw\settings.json`). It lives outside the plugin so plugin updates never overwrite it.
2. **If settings.json does not exist**, use these defaults:
   - `kb_path`: `~/Documents/BrisLaw/opinions` (general folder for saved opinions)
   - `persona`: "You are a senior associate at a major litigation firm specializing in complex commercial litigation. You are working with a well-known partner on an important matter. You analyze cases with precision, identify key holdings and reasoning, and assess practical significance for the client's position."
   - `case_folder_pattern`: not set (folder-aware context is disabled; matter-specific save destinations are unavailable)
   - `default_save_location`: "kb"
   - Inform the user: "BrisLaw settings are not configured, so saved opinions go to `~/Documents/BrisLaw/opinions`. Tell me if you want a different folder, or a folder that holds your case files, and I'll save the setting." If the user gives a folder, write `~/.brislaw/settings.json` with the Write tool.
3. **Expand `~`** in all path values to the user's home directory
4. **Verify paths exist** before using them:
   - If `kb_path` does not exist, create it when saving (the CLI creates missing folders) and tell the user where the file went.
   - If `case_folder_pattern` does not exist, inform the user and note that matter-specific saves are unavailable until the path is configured correctly
5. **Use settings values** throughout the session for save destinations, persona prompt (passed to sub-agents), and default save location

## Settings Schema

| Field | Type | Purpose | Default |
|-------|------|---------|---------|
| `kb_path` | string | Directory for general-reference opinion saves | `~/Documents/BrisLaw/opinions` |
| `case_folder_pattern` | string | Base path for active matter case folders | not set |
| `persona` | string | Persona prompt passed to analysis sub-agents | Senior associate prompt (see defaults above) |
| `default_save_location` | string | Default save destination: `"kb"` or `"matter"` | `"kb"` |

## Settings Command

When the user says `/brislaw settings`:

1. Read `~/.brislaw/settings.json` using the Read tool
2. If the file exists, display all current values in a readable format:
   - Knowledge base path: `[kb_path]`
   - Case folder pattern: `[case_folder_pattern]`
   - Persona: `[first 80 chars]...` (truncate for display)
   - Default save location: `[default_save_location]`
3. If the file does not exist, display the default values and note they are defaults
4. Always end with: "To change these settings, tell me what to change, or edit `~/.brislaw/settings.json` directly."
5. Do NOT implement an interactive setup wizard -- just show current values and the file path
