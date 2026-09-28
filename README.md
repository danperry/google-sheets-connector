# sheets-mcp

A small MCP server that lets Claude read and edit Google Sheets by **column
name** (not cell position), with compact CSV output. Built to run inside
Claude Code cloud sessions and scheduled routines: the cloud session clones
this repo, starts the server from `.mcp.json`, and authenticates with a Google
service account whose key lives in the cloud environment.

| Tool | Does |
|---|---|
| `describe` | Tabs, headers, row counts, last rows, and the sheet's `_notes` instructions |
| `find` | Rows matching `{"Column": "value"}` (`~value` = contains) |
| `append` | Add rows as `{"Column": value}` after the last data row |
| `update` | Set columns on matching rows |
| `delete` | Delete matching rows |
| `cells` | Raw A1 read/write, for anything else |
| `list_sheets` | Your configured sheet aliases |

Tool definitions total about 1.3K tokens.

## Setup

### 1. Google Cloud (browser, ~15 min)
1. Go to <https://console.cloud.google.com>, create a project (e.g. "Claude Sheets").
2. **APIs & Services → Library** → enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts → Create service account**. Name it
   (e.g. `claude-sheets`). Skip the optional role/access steps.
4. Open the new account → **Keys → Add key → Create new key → JSON**. A `.json`
   file downloads. Treat it like a password. Don't put it in this folder.
5. Copy the service account's email (`claude-sheets@<project>.iam.gserviceaccount.com`).

### 2. Share your sheets
Open each Google Sheet you want Claude to use → **Share** → paste the service
account email → **Editor**. Turn off "Notify people". It can only see sheets
shared this way.

### 3. Register the sheets
This repo is public, so your sheet list lives in a cloud environment variable,
not in the repo. Write it like `sheets.example.json`: one entry per sheet,
with the ID from its URL (`docs.google.com/spreadsheets/d/<ID>/edit`) and a
short note. You'll paste it as `SHEETS_CONFIG` in step 5. For local use, save
it as `sheets.json` in this folder instead (it's gitignored).

Optionally add a tab named `_notes` to the sheet itself with plain-English
rules ("One row per item; put 'school' in For for school supplies").

### 4. GitHub
Push this folder to a GitHub repo. Cloud sessions can read public repos
without the Claude GitHub App; install it (<https://github.com/apps/claude>)
if you want sessions to push changes.

### 5. Cloud environment
At <https://claude.ai/code>, open the environment settings (create one, e.g.
"Sheets", with network access **Trusted**). Under **Environment variables** add:

```
GOOGLE_SERVICE_ACCOUNT_JSON=<base64 of the key file>
SHEETS_CONFIG=<base64 of your sheet list>
```

Get each base64 value (copied to your clipboard) with:

```bash
base64 -i ~/Downloads/<key-file>.json | tr -d '\n' | pbcopy
base64 -i sheets.json | tr -d '\n' | pbcopy
```

Note: environment variables are visible to anyone using the environment
(just you on a personal plan) and to Claude inside the session. The key can
only reach sheets you shared with the service account.

### 6. Test
Start a cloud session on this repo in that environment and ask:
"describe my <alias> sheet". Then create a routine (`/schedule` or
claude.ai/code → Routines) on this repo and environment.

## Maintenance

- **Add a sheet:** share it with the service account (Editor, no notify), add
  it to your local `sheets.json`, re-copy it
  (`base64 -i sheets.json | tr -d '\n' | pbcopy`) and replace `SHEETS_CONFIG`
  in the cloud environment. Environment changes only affect sessions started
  afterwards.
- **Never commit the key or `sheets.json`.** This repo is public; Google scans
  GitHub and disables leaked keys. `.gitignore` covers the usual names.
- **Rotate the key** (if leaked, or yearly): Cloud Console → Service Accounts →
  the account → Keys → add a new JSON key, update `GOOGLE_SERVICE_ACCOUNT_JSON`,
  then delete the old key there.
- **Dependencies** are fetched fresh each cloud run (`mcp>=2,<3`). If a new
  release breaks things, pin an exact version in the header of `server.py`.
- **Emails from Google Cloud** about the project (policy changes, inactivity)
  are worth reading; the project is free and has no billing attached.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No `sheets` tools in the session | Server didn't start: session not in this repo, `.mcp.json` missing, or `uv` unavailable. Run `uv run server.py` locally to see errors. |
| `No credentials` | `GOOGLE_SERVICE_ACCOUNT_JSON` missing from the environment, or the session uses a different environment. |
| `MalformedError` / `Incorrect padding` | The pasted key is truncated or has spaces; re-copy with the base64 command. It should be ~3,200 chars starting `ewog`. |
| `Sheet list is not valid JSON` | Re-copy `SHEETS_CONFIG` from a valid `sheets.json`. |
| `Sheets API 403/404` | Sheet not shared with the service account, wrong ID, or Sheets API disabled in the project. |
| `invalid_grant` / `Invalid JWT` | Key was deleted or disabled in Cloud Console; create a new one. |
| `No column 'X'` / `No tab 'X'` | Someone renamed a header or tab; the error lists the current names. |
| Edits made in the browser aren't seen | The cell was still being edited; press Enter. |

## Local testing
```bash
uv run test_server.py                               # offline, fake API
GOOGLE_APPLICATION_CREDENTIALS=~/key.json claude    # real, from this folder
```
