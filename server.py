# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "google-auth>=2.20", "requests>=2.31"]
# ///
"""Header-aware Google Sheets MCP server.

Rows are addressed by column header names instead of cell positions, and
results come back as compact CSV to keep token use low.

Auth (first match wins):
  GOOGLE_SERVICE_ACCOUNT_JSON   service-account key as raw JSON or base64
  GOOGLE_APPLICATION_CREDENTIALS  path to a service-account key file

Sheet aliases (first match wins; kept out of the repo because it's public):
  SHEETS_CONFIG   JSON (or base64 JSON) like sheets.example.json
  sheets.json     local file next to this script (gitignored)
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
NOTES_TAB = "_notes"
ALIASES_FILE = Path(__file__).with_name("sheets.json")
MAX_BULK = 25  # update/delete refuse to touch more rows than this unless all=True

mcp = MCPServer("sheets")
_session = None


# ---------------------------------------------------------------- plumbing

def _http():
    global _session
    if _session is None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if raw:
            info = json.loads(raw if raw.startswith("{") else base64.b64decode(raw))
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        elif os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            creds = service_account.Credentials.from_service_account_file(
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=SCOPES)
        else:
            raise ToolError("No credentials: set GOOGLE_SERVICE_ACCOUNT_JSON "
                               "or GOOGLE_APPLICATION_CREDENTIALS")
        _session = AuthorizedSession(creds)
    return _session


def _call(method: str, path: str, **kw):
    try:
        r = _http().request(method, API + path, timeout=30, **kw)
    except ToolError:
        raise
    except Exception as e:  # bad key format, auth refresh or network failure
        raise ToolError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:300]
        if r.status_code in (403, 404):
            msg += " (is the sheet shared with the service account's email?)"
        raise ToolError(f"Sheets API {r.status_code}: {msg}")
    return r.json()


def _aliases() -> dict:
    """Sheet aliases from SHEETS_CONFIG (JSON or base64 JSON), else sheets.json."""
    raw = os.environ.get("SHEETS_CONFIG", "").strip()
    try:
        if raw:
            return json.loads(raw if raw.startswith("{") else base64.b64decode(raw))
        return json.loads(ALIASES_FILE.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ToolError(f"Sheet list is not valid JSON: {e}") from e


def _sid(spreadsheet: str) -> str:
    """Accept an alias from the sheet list, a full URL, or a raw spreadsheet ID."""
    a = _aliases().get(spreadsheet.strip().lower())
    if a:
        return a["id"] if isinstance(a, dict) else a
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", spreadsheet)
    return m.group(1) if m else spreadsheet.strip()


def _q(tab: str) -> str:
    """Quote a tab name for A1 notation ('My Tab'!A1)."""
    return "'" + tab.replace("'", "''") + "'"


def _col(n: int) -> str:
    """0-based column index -> letters."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _meta(sid: str) -> list[dict]:
    fields = "sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"
    return [s["properties"] for s in _call("GET", f"/{sid}", params={"fields": fields})["sheets"]]


def _tab_props(sid: str, tab: str | None) -> dict:
    tabs = [t for t in _meta(sid) if t["title"] != NOTES_TAB]
    if tab is None:
        return tabs[0]
    for t in tabs:
        if t["title"].lower() == tab.lower():
            return t
    raise ToolError(f"No tab {tab!r}. Tabs: {', '.join(t['title'] for t in tabs)}")


def _values(sid: str, tab: str) -> list[list[str]]:
    rng = quote(_q(tab), safe="")
    return _call("GET", f"/{sid}/values/{rng}").get("values", [])


def _table(sid: str, tab: str):
    """Return (header_row_index, headers, data rows as (sheet_row_number, cells))."""
    vals = _values(sid, tab)
    h = next((i for i, r in enumerate(vals) if any(c.strip() for c in r)), None)
    if h is None:
        return 0, [], []
    headers = [c.strip() for c in vals[h]]
    rows = [(i + 1, r + [""] * (len(headers) - len(r)))
            for i, r in enumerate(vals) if i > h and any(c.strip() for c in r)]
    return h, headers, rows


def _colidx(headers: list[str], name: str) -> int:
    for i, hname in enumerate(headers):
        if hname.lower() == name.strip().lower():
            return i
    raise ToolError(f"No column {name!r}. Columns: {', '.join(h for h in headers if h)}")


def _match(headers, row, where: dict | None) -> bool:
    if not where:
        return True
    for k, v in where.items():
        cell = row[_colidx(headers, k)].strip().lower()
        v = str(v).strip().lower()
        if v.startswith("~"):
            if v[1:] not in cell:
                return False
        elif cell != v:
            return False
    return True


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().rstrip("\n")


def _notes(sid: str, titles: list[str]) -> str:
    if NOTES_TAB not in titles:
        return ""
    return "\n".join(" ".join(c for c in r if c) for r in _values(sid, NOTES_TAB)).strip()


# ---------------------------------------------------------------- tools

@mcp.tool()
def describe(spreadsheet: str, tab: str | None = None, samples: int = 3) -> str:
    """Layout of a spreadsheet: tabs, column headers, row counts, a few sample
    rows, and any instructions in its _notes tab. Call this first.
    spreadsheet: alias (see list_sheets), URL, or ID. tab: limit to one tab."""
    sid = _sid(spreadsheet)
    meta = _meta(sid)
    titles = [t["title"] for t in meta]
    out = []
    alias_note = next((v.get("notes") for k, v in _aliases().items()
                       if isinstance(v, dict) and v.get("id") == sid and v.get("notes")), None)
    if alias_note:
        out.append(f"NOTES: {alias_note}")
    n = _notes(sid, titles)
    if n:
        out.append(f"NOTES ({NOTES_TAB} tab): {n}")
    for t in meta:
        if t["title"] == NOTES_TAB or (tab and t["title"].lower() != tab.lower()):
            continue
        h, headers, rows = _table(sid, t["title"])
        out.append(f"\n## {t['title']}  ({len(rows)} data rows; header on row {h + 1})")
        out.append(_csv([headers]) if headers else "(empty)")
        if rows and samples:
            out.append("last rows (sheet row # first):\n"
                       + _csv([[n] + r for n, r in rows[-samples:]]))
    return "\n".join(out).strip()


@mcp.tool()
def find(spreadsheet: str, tab: str | None = None, where: dict | None = None,
         columns: list[str] | None = None, limit: int = 100) -> str:
    """Rows matching `where` as CSV with the sheet row number first.
    where: {"Column": "value"} case-insensitive exact match; prefix a value
    with ~ for contains (e.g. {"Item": "~pencil"}). Omit where for all rows.
    columns: only return these columns."""
    sid = _sid(spreadsheet)
    tname = _tab_props(sid, tab)["title"]
    _, headers, rows = _table(sid, tname)
    idx = [_colidx(headers, c) for c in columns] if columns else list(range(len(headers)))
    hits = [(n, r) for n, r in rows if _match(headers, r, where)]
    body = [["#"] + [headers[i] for i in idx]] + [[n] + [r[i] for i in idx] for n, r in hits[:limit]]
    more = f"\n({len(hits) - limit} more not shown)" if len(hits) > limit else ""
    return f"{len(hits)} match(es) in {tname}\n" + _csv(body) + more


@mcp.tool()
def append(spreadsheet: str, rows: list[dict], tab: str | None = None) -> str:
    """Add rows after the last row of data. Each row is {"Column": value};
    columns you omit stay blank. Values are entered as if typed (dates,
    numbers, formulas, TRUE/FALSE for checkboxes work)."""
    sid = _sid(spreadsheet)
    props = _tab_props(sid, tab)
    tname = props["title"]
    h, headers, data = _table(sid, tname)
    if not headers:
        raise ToolError(f"Tab {tname!r} has no header row")
    grid = []
    for r in rows:
        line = [""] * len(headers)
        for k, v in r.items():
            line[_colidx(headers, k)] = "" if v is None else v
        grid.append(line)
    start = (data[-1][0] if data else h + 1) + 1
    end = start + len(grid) - 1
    if end > props["gridProperties"]["rowCount"]:
        _call("POST", f"/{sid}:batchUpdate", json={"requests": [{"appendDimension": {
            "sheetId": props["sheetId"], "dimension": "ROWS",
            "length": end - props["gridProperties"]["rowCount"]}}]})
    rng = f"{_q(tname)}!A{start}:{_col(len(headers) - 1)}{end}"
    _call("PUT", f"/{sid}/values/{quote(rng, safe='')}",
          params={"valueInputOption": "USER_ENTERED"}, json={"values": grid})
    return f"Added {len(grid)} row(s) to {tname} at rows {start}-{end}"


@mcp.tool()
def update(spreadsheet: str, where: dict, set: dict, tab: str | None = None,
           all: bool = False) -> str:
    """Set columns on every row matching `where` (same matching as find).
    set: {"Column": new_value}. Refuses to change more than 25 rows unless all=True."""
    sid = _sid(spreadsheet)
    tname = _tab_props(sid, tab)["title"]
    _, headers, rows = _table(sid, tname)
    hits = [n for n, r in rows if _match(headers, r, where)]
    if not hits:
        return "0 rows matched; nothing changed"
    if len(hits) > MAX_BULK and not all:
        return f"{len(hits)} rows match; pass all=True to change them all"
    cols = {_colidx(headers, k): v for k, v in set.items()}
    data = [{"range": f"{_q(tname)}!{_col(c)}{n}", "values": [[v]]}
            for n in hits for c, v in cols.items()]
    _call("POST", f"/{sid}/values:batchUpdate",
          json={"valueInputOption": "USER_ENTERED", "data": data})
    return f"Updated {len(hits)} row(s) in {tname}: rows {', '.join(map(str, hits))}"


@mcp.tool()
def delete(spreadsheet: str, where: dict, tab: str | None = None, all: bool = False) -> str:
    """Delete every row matching `where` (same matching as find); rows below
    shift up. Refuses to delete more than 25 rows unless all=True."""
    sid = _sid(spreadsheet)
    props = _tab_props(sid, tab)
    _, headers, rows = _table(sid, props["title"])
    hits = [n for n, r in rows if _match(headers, r, where)]
    if not hits:
        return "0 rows matched; nothing deleted"
    if len(hits) > MAX_BULK and not all:
        return f"{len(hits)} rows match; pass all=True to delete them all"
    reqs = [{"deleteDimension": {"range": {"sheetId": props["sheetId"], "dimension": "ROWS",
                                           "startIndex": n - 1, "endIndex": n}}}
            for n in sorted(hits, reverse=True)]
    _call("POST", f"/{sid}:batchUpdate", json={"requests": reqs})
    return f"Deleted {len(hits)} row(s) from {props['title']}: rows {', '.join(map(str, hits))}"


@mcp.tool()
def cells(spreadsheet: str, range: str, values: list[list] | None = None) -> str:
    """Escape hatch for anything the other tools can't do. Reads an A1 range
    (e.g. "'Off island'!B2:D9") as CSV, or writes `values` (2D list) to it
    as if typed."""
    sid = _sid(spreadsheet)
    enc = quote(range, safe="")
    if values is None:
        return _csv(_call("GET", f"/{sid}/values/{enc}").get("values", [])) or "(empty)"
    r = _call("PUT", f"/{sid}/values/{enc}",
              params={"valueInputOption": "USER_ENTERED"}, json={"values": values})
    return f"Wrote {r.get('updatedCells', 0)} cell(s) to {r.get('updatedRange', range)}"


@mcp.tool()
def list_sheets() -> str:
    """Configured spreadsheet aliases, with their notes."""
    a = _aliases()
    if not a:
        return "No aliases configured; pass a spreadsheet URL or ID instead."
    return "\n".join(f"{k}: {v.get('notes', '') if isinstance(v, dict) else ''}".rstrip(": ")
                     for k, v in a.items())


if __name__ == "__main__":
    mcp.run()
