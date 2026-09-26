# Sheets helper

This repo provides the `sheets` MCP server (server.py) for editing Google Sheets.
Sessions here, including scheduled routines, use it to read and change sheets.

## How to work with a sheet
1. Call `describe` first (once per session per sheet). It returns tabs, headers,
   row counts, the last few rows, and the sheet's own instructions from its
   `_notes` tab. Follow those instructions.
2. Address data by column header, never by cell position:
   - add: `append(rows=[{"Item": "...", "For": "school"}])`
   - look up: `find(where={"Item": "~pencil"})` (`~` = contains)
   - change: `update(where={...}, set={...})`
   - remove: `delete(where={...})`
3. Use `cells` only when the other tools can't express the change.
4. Before appending, `find` to avoid adding a duplicate of an existing row.
5. `spreadsheet` accepts an alias (`list_sheets`), a sheet URL,
   or an ID. Prefer aliases.

## Adding a sheet
Share it with the service account's email (Editor), then add an alias to the
`SHEETS_CONFIG` cloud environment variable (format: sheets.example.json) with
its ID (the part of the URL between `/d/` and `/edit`) and a one-line note.
This repo is public: never commit sheet IDs, notes, or a sheets.json file.

## Never
- Print, log, or commit the service-account key, `GOOGLE_SERVICE_ACCOUNT_JSON`,
  or `SHEETS_CONFIG`.
- Delete or bulk-update rows the task didn't ask for.
