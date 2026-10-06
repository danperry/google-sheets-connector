# Sheets helper

This repo provides the `sheets` MCP server (server.py) for editing Google Sheets
and Google Docs.
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

## How to work with a doc
1. `doc_read` first; pass `section="<heading>"` to read just one part.
2. Edit with `doc_edit`, which changes text in place and keeps formatting:
   - change wording: `doc_edit(replace={"old text": "new text"})` (every
     occurrence; `""` deletes). Make the old text specific enough to hit only
     what the task means.
   - add: `doc_edit(insert="line 1\nline 2", after="Heading")` (`~` = contains);
     omit `after` to add at the end.
   - fill a table (first row = headers): `doc_edit(table="Heading above it",
     rows=[{"Room": "2", "Tenant name": "..."}])` fills the first empty rows,
     adding rows if needed; `doc_edit(table=..., where={"Tenant name": "~ana"},
     set={"Expected rent": "$900"})` changes cells on matching rows.
3. Links: write `[UNSUBSCRIBE](https://...)` to show a short word linked to the
   URL; `doc_read` shows existing links the same way, and `replace` matches that
   form. Bare `http(s)://` URLs become clickable links after any `doc_edit`.
4. Never recreate a doc to change it; its ID and history must stay.

## Adding a sheet or doc
Share it with the service account's email (Editor), then add an alias to the
`SHEETS_CONFIG` cloud environment variable (plain one-line JSON, format:
sheets.example.json; the user edits it in the environment settings) with
its ID (the part of the URL between `/d/` and `/edit`) and a one-line note.
This repo is public: never commit sheet IDs, notes, or a sheets.json file.

## When something fails
Check the Troubleshooting table in README.md, quote the exact error, and tell
the user which fix applies. Don't try to work around a credential error.

## Never
- Print, log, or commit the service-account key, `GOOGLE_SERVICE_ACCOUNT_JSON`,
  or `SHEETS_CONFIG`.
- Delete or bulk-update rows the task didn't ask for.
