# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "google-auth>=2.20", "requests>=2.31"]
# ///
"""Header-aware Google Sheets MCP server, plus read/edit for Google Docs.

Rows are addressed by column header names instead of cell positions, and
results come back as compact CSV to keep token use low. Docs are read as
plain text with Markdown headings and edited by text, never by index.

Auth (first match wins):
  GOOGLE_SERVICE_ACCOUNT_JSON   service-account key as raw JSON or base64
  GOOGLE_APPLICATION_CREDENTIALS  path to a service-account key file

Aliases for sheets and docs (first match wins; kept out of the repo because it's public):
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
DOCS_API = "https://docs.googleapis.com/v1/documents"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/documents"]
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


def _call(method: str, path: str, api: str = API, **kw):
    try:
        r = _http().request(method, api + path, timeout=30, **kw)
    except ToolError:
        raise
    except Exception as e:  # bad key format, auth refresh or network failure
        raise ToolError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:300]
        if r.status_code in (403, 404) and "API has not been used" not in msg:
            msg += " (is it shared with the service account's email?)"
        name = "Docs API" if api == DOCS_API else "Sheets API"
        raise ToolError(f"{name} {r.status_code}: {msg}")
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
    """Accept an alias from the sheet list, a full URL, or a raw sheet/doc ID."""
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


def _hit(text: str, pattern) -> bool:
    """Case-insensitive exact match, or contains when pattern starts with ~."""
    text, pattern = text.strip().lower(), str(pattern).strip().lower()
    return pattern[1:] in text if pattern.startswith("~") else text == pattern


def _match(headers, row, where: dict | None) -> bool:
    return all(_hit(row[_colidx(headers, k)], v) for k, v in (where or {}).items())


def _csv(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().rstrip("\n")


def _alias_note(sid: str) -> str | None:
    return next((v.get("notes") for v in _aliases().values()
                 if isinstance(v, dict) and v.get("id") == sid and v.get("notes")), None)


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
    alias_note = _alias_note(sid)
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


# ---------------------------------------------------------------- docs

HEADINGS = {"TITLE": 1, **{f"HEADING_{n}": n for n in range(1, 7)}}


def _u16(s: str) -> int:
    """Length in UTF-16 code units, which is how the Docs API counts indices."""
    return len(s.encode("utf-16-le")) // 2


def _doc_tab(did: str, tab: str | None):
    """Return (doc title, all tab titles, the chosen tab)."""
    d = _call("GET", f"/{did}", api=DOCS_API, params={"includeTabsContent": "true"})
    tabs, todo = [], list(d.get("tabs", []))
    while todo:
        t = todo.pop(0)
        tabs.append(t)
        todo[:0] = t.get("childTabs", [])
    titles = [t["tabProperties"]["title"] for t in tabs]
    if tab is None:
        return d.get("title", ""), titles, tabs[0]
    for t in tabs:
        if t["tabProperties"]["title"].lower() == tab.strip().lower():
            return d.get("title", ""), titles, t
    raise ToolError(f"No tab {tab!r}. Tabs: {', '.join(titles)}")


def _para_text(p: dict) -> str:
    return "".join(e.get("textRun", {}).get("content", "") for e in p["elements"]).rstrip("\n")


def _blocks(t: dict) -> list[dict]:
    """Top-level paragraphs and tables of a tab, with their index range and text."""
    out = []
    for el in t["documentTab"]["body"]["content"]:
        if "paragraph" in el:
            p = el["paragraph"]
            text = _para_text(p)
            level = HEADINGS.get(p.get("paragraphStyle", {}).get("namedStyleType"))
            if level and text.strip():
                line = "#" * level + " " + text
            elif "bullet" in p:
                line = "  " * p["bullet"].get("nestingLevel", 0) + "- " + text
            else:
                line = text
            out.append({"start": el["startIndex"], "end": el["endIndex"], "text": text,
                        "level": level if text.strip() else None, "heading": bool(level),
                        "line": line})
        elif "table" in el:
            rows = ["| " + " | ".join(" ".join(_para_text(c["paragraph"])
                                               for c in cell["content"] if "paragraph" in c)
                                      for cell in row["tableCells"]) + " |"
                    for row in el["table"]["tableRows"]]
            out.append({"start": el["startIndex"], "end": el["endIndex"], "text": "",
                        "level": None, "heading": False, "line": "\n".join(rows)})
    return out


def _cell(c: dict) -> dict:
    """A table cell's text and the index range that holds it (minus the final newline)."""
    paras = [e for e in c["content"] if "paragraph" in e]
    return {"text": " ".join(_para_text(e["paragraph"]) for e in paras),
            "start": c["content"][0]["startIndex"], "end": c["content"][-1]["endIndex"] - 1}


def _tables(t: dict) -> list[dict]:
    """Top-level tables of a tab: header cells, data rows of cells, and the
    nearest non-empty paragraph above each (usually its heading)."""
    out, above = [], ""
    for el in t["documentTab"]["body"]["content"]:
        if "paragraph" in el:
            above = _para_text(el["paragraph"]).strip() or above
        elif "table" in el:
            rows = [[_cell(c) for c in r["tableCells"]] for r in el["table"]["tableRows"]]
            out.append({"start": el["startIndex"], "above": above,
                        "headers": [c["text"].strip() for c in rows[0]], "rows": rows[1:]})
    return out


def _find_table(t: dict, table: str | None) -> dict:
    tables = _tables(t)
    if table is None:
        hits = tables
    else:
        hits = [x for x in tables if (x["above"] and _hit(x["above"], table))
                or any(_hit(h, table) for h in x["headers"])]
    if len(hits) == 1:
        return hits[0]
    names = "; ".join(f"{x['above'][:40]!r} ({', '.join(x['headers'])})" for x in tables)
    if not tables:
        raise ToolError("This tab has no tables")
    if not hits:
        raise ToolError(f"No table matching {table!r}. Tables: {names}")
    raise ToolError(f"{len(hits)} tables match; pass table=<heading above it or a "
                    f"column name> to pick one. Tables: {names}")


def _find_block(blocks: list[dict], after: str) -> dict:
    hits = [b for b in blocks if b["text"] and _hit(b["text"], after)]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        heads = [b["text"] for b in blocks if b["level"]]
        raise ToolError(f"No paragraph matching {after!r}. Headings: {', '.join(heads) or '(none)'}")
    raise ToolError(f"{after!r} matches {len(hits)} paragraphs; be more specific: "
                    + "; ".join(repr(b["text"][:60]) for b in hits[:5]))


@mcp.tool()
def doc_read(document: str, section: str | None = None, tab: str | None = None) -> str:
    """Read a Google Doc as plain text with Markdown headings (#), bullets (-)
    and tables (| a | b |). Formatting, images and comments are left out.
    document: alias (see list_sheets), URL, or ID.
    section: only the part under this heading (exact, case-insensitive; ~ for
    contains). tab: which document tab (default: the first)."""
    did = _sid(document)
    title, titles, t = _doc_tab(did, tab)
    blocks = _blocks(t)
    if section:
        start = next((i for i, b in enumerate(blocks) if b["level"] and _hit(b["text"], section)), None)
        if start is None:
            heads = [b["text"] for b in blocks if b["level"]]
            raise ToolError(f"No heading matching {section!r}. Headings: {', '.join(heads) or '(none)'}")
        lvl = blocks[start]["level"]
        end = next((i for i in range(start + 1, len(blocks))
                    if blocks[i]["level"] and blocks[i]["level"] <= lvl), len(blocks))
        blocks = blocks[start:end]
    head = [f"DOC: {title}"]
    if len(titles) > 1:
        head.append(f"TAB: {t['tabProperties']['title']} (tabs: {', '.join(titles)})")
    note = _alias_note(did)
    if note:
        head.append(f"NOTES: {note}")
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(b["line"] for b in blocks)).strip()
    return "\n".join(head) + "\n\n" + (body or "(empty)")


@mcp.tool()
def doc_edit(document: str, replace: dict | None = None, insert: str | None = None,
             after: str | None = None, tab: str | None = None, match_case: bool = False,
             all: bool = False, table: str | None = None, rows: list[dict] | None = None,
             where: dict | None = None, set: dict | None = None) -> str:
    """Change a Google Doc in place; the rest of the doc and its formatting stay put.
    replace: {"old text": "new text"} replaces every occurrence ("" deletes it).
      Refuses a phrase that occurs more than 25 times unless all=True.
    insert: text to add as new paragraph(s); each line becomes a paragraph.
      Goes right after the paragraph matching `after` (a heading or line; exact,
      case-insensitive, ~ for contains), or at the end of the doc if omitted.
      New paragraphs copy that paragraph's style (after a list item they join
      the list), except after a heading, where they are normal text.
    Tables, addressed by column header (the table's first row) like a sheet:
      table: the heading/line just above the table, or one of its column
        names (~ for contains); may be omitted when the tab has one table.
      rows: [{"Column": "text"}] fills the first fully empty rows, in order,
        and adds rows at the bottom when there aren't enough.
      where + set: set cells on every row matching where (same matching as
        find), replacing what's in them ("" clears). Refuses more than 25
        rows unless all=True.
    tab: which document tab (default: the first)."""
    if not replace and not (insert or "").strip() and not rows and not set:
        raise ToolError("Nothing to do: pass replace, insert, rows, or where + set")
    if bool(where) != bool(set):
        raise ToolError("where and set go together: where picks rows, set gives the new cell text")
    did = _sid(document)
    _, _, t = _doc_tab(did, tab)
    tid = t["tabProperties"]["tabId"]
    done = []
    if replace:
        text = "\n".join(b["line"] for b in _blocks(t))
        for old in replace:
            n = text.count(old) if match_case else text.lower().count(old.lower())
            if n > MAX_BULK and not all:
                return f"{old!r} occurs {n} times; pass all=True to replace them all. Nothing changed."
        reqs = [{"replaceAllText": {"containsText": {"text": old, "matchCase": match_case},
                                    "replaceText": "" if new is None else str(new),
                                    "tabsCriteria": {"tabIds": [tid]}}}
                for old, new in replace.items()]
        r = _call("POST", f"/{did}:batchUpdate", api=DOCS_API, json={"requests": reqs})
        for old, rep in zip(replace, r.get("replies", [])):
            done.append(f"replaced {old!r} x{rep.get('replaceAllText', {}).get('occurrencesChanged', 0)}")
        if insert or rows or set:
            _, _, t = _doc_tab(did, tab)
    if insert and insert.strip():
        blocks = _blocks(t)
        anchor = _find_block(blocks, after) if after else blocks[-1]
        idx = anchor["end"] - 1  # just before the anchor paragraph's own newline
        ins = insert.strip("\n")
        prefix = "" if not after and not anchor["text"] else "\n"
        reqs = [{"insertText": {"location": {"index": idx, "tabId": tid}, "text": prefix + ins}}]
        if anchor["heading"]:
            start = idx + _u16(prefix)
            reqs.append({"updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": start + _u16(ins), "tabId": tid},
                "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}, "fields": "namedStyleType"}})
        _call("POST", f"/{did}:batchUpdate", api=DOCS_API, json={"requests": reqs})
        spot = f"after {anchor['text'][:60]!r}" if after else "at the end"
        done.append(f"inserted {ins.count(chr(10)) + 1} paragraph(s) {spot}")
        if rows or set:
            _, _, t = _doc_tab(did, tab)
    if rows or set:
        done.append(_fill_table(did, tab, tid, t, table, rows, where, set, force=all))
    return "Done: " + "; ".join(done)


def _fill_table(did, tab, tid, t, table, rows, where, set, force) -> str:
    x = _find_table(t, table)
    heads = x["headers"]
    for k in [k for r in rows or [] for k in r] + list(set or {}) + list(where or {}):
        _colidx(heads, k)  # unknown column: fail before changing anything
    writes, touched = [], []
    if set:
        hits = [n for n, r in enumerate(x["rows"])
                if all(_hit(r[_colidx(heads, k)]["text"], v) for k, v in where.items())]
        if not hits and not rows:
            return "0 table rows matched; nothing changed"
        if len(hits) > MAX_BULK and not force:
            return f"{len(hits)} table rows match; pass all=True to change them all. Nothing changed."
        writes += [(x["rows"][n][_colidx(heads, k)], v) for n in hits for k, v in set.items()]
        touched += hits
    if rows:
        blank = [n for n, r in enumerate(x["rows"]) if not any(c["text"].strip() for c in r)]
        if len(rows) > len(blank):
            loc = {"tableStartLocation": {"index": x["start"], "tabId": tid},
                   "rowIndex": len(x["rows"]), "columnIndex": 0}
            _call("POST", f"/{did}:batchUpdate", api=DOCS_API, json={"requests": [
                {"insertTableRow": {"tableCellLocation": loc, "insertBelow": True}}
                for _ in range(len(rows) - len(blank))]})
            _, _, t = _doc_tab(did, tab)
            x = next(y for y in _tables(t) if y["start"] == x["start"])
            if set:  # re-read cells from the refreshed table
                writes = [(x["rows"][n][_colidx(heads, k)], v) for n in touched for k, v in set.items()]
            blank = [n for n, r in enumerate(x["rows"]) if not any(c["text"].strip() for c in r)]
        for n, r in zip(blank, rows):
            writes += [(x["rows"][n][_colidx(heads, k)], v) for k, v in r.items()]
            touched.append(n)
    reqs = []
    for c, v in sorted(writes, key=lambda w: w[0]["start"], reverse=True):
        if c["end"] > c["start"]:
            reqs.append({"deleteContentRange": {"range": {
                "startIndex": c["start"], "endIndex": c["end"], "tabId": tid}}})
        if v not in (None, ""):
            reqs.append({"insertText": {"location": {"index": c["start"], "tabId": tid},
                                        "text": str(v)}})
    if reqs:
        _call("POST", f"/{did}:batchUpdate", api=DOCS_API, json={"requests": reqs})
    name = x["above"][:60] or ", ".join(heads)
    return (f"wrote {len(writes)} cell(s) in table {name!r}, row(s) "
            + ", ".join(str(n + 1) for n in sorted(dict.fromkeys(touched))))


@mcp.tool()
def list_sheets() -> str:
    """Configured sheet and doc aliases, with their notes."""
    a = _aliases()
    if not a:
        return "No aliases configured; pass a spreadsheet URL or ID instead."
    return "\n".join(f"{k}: {v.get('notes', '') if isinstance(v, dict) else ''}".rstrip(": ")
                     for k, v in a.items())


if __name__ == "__main__":
    mcp.run()
