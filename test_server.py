# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "google-auth>=2.20", "requests>=2.31"]
# ///
"""Offline tests: runs every tool against in-memory fakes of the Sheets and Docs APIs.
Run with: uv run test_server.py"""
import re
from urllib.parse import unquote

import server
from server import ToolError

SID = "abc123"


def col_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


DID = "doc456"


class FakeDocs:
    """A doc as a list of paragraphs and tables, indexed the way the Docs API does:
    index 0 is the section break, each paragraph ends with its own newline, and a
    table, each of its rows and each cell take one index before their content,
    plus one at the table's end. A table is {"table": [[[cell paragraph texts]]]}."""

    def __init__(self, paras=None):
        self.paras = paras or [
            {"text": "Farm plan", "style": "TITLE", "bullet": None},
            {"text": "Chores", "style": "HEADING_1", "bullet": None},
            {"text": "Feed hens", "style": "NORMAL_TEXT", "bullet": 0},
            {"text": "Water beds \U0001F331", "style": "NORMAL_TEXT", "bullet": 0},
            {"text": "Budget", "style": "HEADING_1", "bullet": None},
            {"text": "Spend less on hay. Hay is dear.", "style": "NORMAL_TEXT", "bullet": None},
            {"text": "Later", "style": "HEADING_1", "bullet": None},
        ]
        self.calls = []

    def layout(self):
        """(API body content, every paragraph as (start, end, its list, its position))."""
        content, slots, i = [{"startIndex": 0, "endIndex": 1, "sectionBreak": {}}], [], 1

        def para(p, lst, k):
            nonlocal i
            n = server._u16(p["text"]) + 1
            el = {"startIndex": i, "endIndex": i + n, "paragraph": {
                "elements": self.runs(p, i),
                "paragraphStyle": {"namedStyleType": p.get("style", "NORMAL_TEXT")}}}
            if p.get("bullet") is not None:
                el["paragraph"]["bullet"] = {"nestingLevel": p["bullet"]}
            slots.append((i, i + n, lst, k))
            i += n
            return el

        for k, p in enumerate(self.paras):
            if "table" not in p:
                content.append(para(p, self.paras, k))
                continue
            t0, rows = i, []
            i += 1
            for row in p["table"]:
                r0, cells = i, []
                i += 1
                for cell in row:
                    c0 = i
                    i += 1
                    cc = [para(q, cell, j) for j, q in enumerate(cell)]
                    cells.append({"startIndex": c0, "endIndex": i, "content": cc})
                rows.append({"startIndex": r0, "endIndex": i, "tableCells": cells})
            i += 1
            content.append({"startIndex": t0, "endIndex": i, "table": {"tableRows": rows}})
        return content, slots

    def runs(self, p, i):
        """Text runs of a paragraph starting at index i, split where links begin and end."""
        units, links = (p["text"] + "\n").encode("utf-16-le"), sorted(p.get("links", []))
        cuts = sorted({0, len(units) // 2, *(x for a, b, _ in links for x in (a, b))})
        out = []
        for a, b in zip(cuts, cuts[1:]):
            run = {"content": units[a * 2:b * 2].decode("utf-16-le")}
            url = next((u for la, lb, u in links if la <= a and b <= lb), None)
            if url:
                run["textStyle"] = {"link": {"url": url}}
            out.append({"startIndex": i + a, "endIndex": i + b, "textRun": run})
        return out

    def get(self):
        return {"title": "Farm plan", "tabs": [{"tabProperties": {"tabId": "t.0", "title": "Tab 1"},
                "documentTab": {"body": {"content": self.layout()[0]}}}]}

    def insert(self, index, text):
        a, _, lst, k = next(s for s in self.layout()[1] if s[0] <= index < s[1])
        p = lst[k]
        units = p["text"].encode("utf-16-le")
        off = (index - a) * 2
        full = (units[:off].decode("utf-16-le") + text + units[off:].decode("utf-16-le"))
        if "\n" not in text:  # links after the insertion point move right, like the real API
            n, x = len(text.encode("utf-16-le")) // 2, index - a
            p = dict(p, links=[(la + n * (la >= x), lb + n * (lb >= x), u) for la, lb, u in p.get("links", [])])
        lst[k:k + 1] = [dict(p, text=line) for line in full.split("\n")]

    def delete(self, a0, b0):
        """Delete within one list of paragraphs (one cell, in practice); keeps its last newline."""
        hit = [s for s in self.layout()[1] if s[0] < b0 and a0 < s[1]]
        if len(hit) == 1 and b0 < hit[0][1]:  # inside one paragraph
            a, _, lst, k = hit[0]
            units = lst[k]["text"].encode("utf-16-le")
            lst[k]["text"] = (units[:(a0 - a) * 2] + units[(b0 - a) * 2:]).decode("utf-16-le")
            d, x0, x1 = b0 - a0, a0 - a, b0 - a  # links after the cut move left, like the real API
            lst[k]["links"] = [(la - d * (la >= x1), lb - d * (lb >= x1), u)
                               for la, lb, u in lst[k].get("links", []) if lb <= x0 or la >= x1]
            return
        lst = hit[0][2]
        assert all(s[2] is lst for s in hit), "delete spans containers"
        base = next(s[0] for s in self.layout()[1] if s[2] is lst and s[3] == 0)
        units = "\n".join(p["text"] for p in lst).encode("utf-16-le")
        assert b0 - base <= len(units) // 2, "would delete the cell's last newline"
        full = (units[:(a0 - base) * 2] + units[(b0 - base) * 2:]).decode("utf-16-le")
        lst[:] = [dict(lst[0], text=line) for line in full.split("\n")]

    def __call__(self, method, path, **kw):
        self.calls.append((method, path, kw))
        if method == "GET" and path == f"/{DID}":
            assert kw["params"]["includeTabsContent"] == "true"
            return self.get()
        assert method == "POST" and path == f"/{DID}:batchUpdate", path
        replies = []
        for r in kw["json"]["requests"]:
            if "replaceAllText" in r:
                q = r["replaceAllText"]
                old, new = q["containsText"]["text"], q["replaceText"]
                assert q["tabsCriteria"]["tabIds"] == ["t.0"]
                pat = re.compile(re.escape(old), 0 if q["containsText"]["matchCase"] else re.I)
                n = 0
                for _, _, lst, k in self.layout()[1]:
                    lst[k]["text"], m = pat.subn(new, lst[k]["text"])
                    n += m
                replies.append({"replaceAllText": {"occurrencesChanged": n}})
            elif "insertText" in r:
                loc = r["insertText"]["location"]
                assert loc["tabId"] == "t.0"
                self.insert(loc["index"], r["insertText"]["text"])
                replies.append({})
            elif "deleteContentRange" in r:
                rng = r["deleteContentRange"]["range"]
                assert rng["tabId"] == "t.0"
                self.delete(rng["startIndex"], rng["endIndex"])
                replies.append({})
            elif "updateTextStyle" in r:
                q = r["updateTextStyle"]
                rng = q["range"]
                assert rng["tabId"] == "t.0" and q["fields"] == "link"
                a, b, lst, k = next(s for s in self.layout()[1] if s[0] <= rng["startIndex"] < s[1])
                assert rng["endIndex"] < b, "link runs past its paragraph"
                lst[k].setdefault("links", []).append(
                    (rng["startIndex"] - a, rng["endIndex"] - a, q["textStyle"]["link"]["url"]))
                replies.append({})
            elif "insertTableRow" in r:
                loc = r["insertTableRow"]["tableCellLocation"]
                assert loc["tableStartLocation"]["tabId"] == "t.0" and r["insertTableRow"]["insertBelow"]
                content = self.layout()[0]
                k = next(k for k, el in enumerate(content[1:]) if el["startIndex"] == loc["tableStartLocation"]["index"])
                tbl = self.paras[k]["table"]
                tbl.insert(loc["rowIndex"] + 1, [[{"text": ""}] for _ in tbl[0]])
                replies.append({})
            elif "updateParagraphStyle" in r:
                u = r["updateParagraphStyle"]
                a0, b0 = u["range"]["startIndex"], u["range"]["endIndex"]
                for a, b, lst, k in self.layout()[1]:
                    if a < b0 and a0 < b:
                        lst[k]["style"] = u["paragraphStyle"]["namedStyleType"]
                replies.append({})
            else:
                raise AssertionError(f"unexpected request {r}")
        return {"replies": replies}


docs = FakeDocs()


class FakeSheets:
    def __init__(self):
        self.tabs = {
            "Off island": {"sheetId": 1, "rowCount": 6, "columnCount": 4, "values": [
                ["Item", "Store", "For", "Bought"],
                ["Almond extract", "", "", "FALSE"],
                ["Yoga mat", "Costco", "", "FALSE"],
                ["Kimchi flakes", "H Mart", "", "TRUE"],
            ]},
            "_notes": {"sheetId": 2, "rowCount": 5, "columnCount": 1, "values": [
                ["One row per item. Put school in For for school supplies."],
            ]},
        }

    def tab_of(self, rng):
        m = re.match(r"^'((?:[^']|'')*)'(?:!(.*))?$", rng)
        assert m, f"range not quoted: {rng}"
        return m.group(1).replace("''", "'"), m.group(2)

    def __call__(self, method, path, api=server.API, **kw):
        if api == server.DOCS_API:
            return docs(method, path, **kw)
        if method == "GET" and path == f"/{SID}":
            return {"sheets": [{"properties": {"sheetId": t["sheetId"], "title": k,
                    "gridProperties": {"rowCount": t["rowCount"], "columnCount": t["columnCount"]}}}
                    for k, t in self.tabs.items()]}
        m = re.match(rf"^/{SID}/values/(.+)$", path)
        if m:
            tab, a1 = self.tab_of(unquote(m.group(1)))
            t = self.tabs[tab]
            if method == "GET":
                if a1 is None:
                    return {"values": [list(r) for r in t["values"]]}
                c0, r0, c1, r1 = re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", a1).groups()
                return {"values": [r[col_num(c0):col_num(c1) + 1]
                                   for r in t["values"][int(r0) - 1:int(r1)]]}
            self.write(t, a1, kw["json"]["values"])
            return {"updatedCells": sum(map(len, kw["json"]["values"])), "updatedRange": a1}
        if path == f"/{SID}/values:batchUpdate":
            for d in kw["json"]["data"]:
                tab, a1 = self.tab_of(d["range"])
                self.write(self.tabs[tab], a1, d["values"])
            return {}
        if path == f"/{SID}:batchUpdate":
            for r in kw["json"]["requests"]:
                t = next(t for t in self.tabs.values()
                         if t["sheetId"] == (r.get("appendDimension") or r["deleteDimension"]["range"])["sheetId"])
                if "appendDimension" in r:
                    t["rowCount"] += r["appendDimension"]["length"]
                else:
                    rg = r["deleteDimension"]["range"]
                    del t["values"][rg["startIndex"]:rg["endIndex"]]
                    t["rowCount"] -= 1
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def write(self, t, a1, values):
        m = re.match(r"^([A-Z]+)(\d+)", a1)
        c0, r0 = col_num(m.group(1)), int(m.group(2)) - 1
        assert r0 + len(values) <= t["rowCount"], "write past grid limit"
        for i, row in enumerate(values):
            while len(t["values"]) <= r0 + i:
                t["values"].append([])
            line = t["values"][r0 + i]
            for j, v in enumerate(row):
                while len(line) <= c0 + j:
                    line.append("")
                line[c0 + j] = str(v)


import base64, json, os
cfg = {"shopping": {"id": SID, "notes": "Family off-island list"}}
for raw in (json.dumps(cfg), base64.b64encode(json.dumps(cfg).encode()).decode()):
    os.environ["SHEETS_CONFIG"] = raw
    assert server._sid("Shopping") == SID
os.environ["SHEETS_CONFIG"] = "{not json"
try:
    server._aliases()
    raise AssertionError("expected error")
except ToolError as e:
    print("error ok:", e)
del os.environ["SHEETS_CONFIG"]

fake = FakeSheets()
server._call = fake
server._aliases = lambda: {"shopping": {"id": SID, "notes": "Family off-island list"}}
vals = lambda: fake.tabs["Off island"]["values"]

out = server.describe("shopping")
print(out, "\n---")
assert "Family off-island list" in out and "Put school in For" in out
assert "Item,Store,For,Bought" in out and "3 data rows" in out and "_notes\n" not in out

out = server.find("https://docs.google.com/spreadsheets/d/abc123/edit#gid=0",
                  where={"bought": "false"}, columns=["Item"])
print(out, "\n---")
assert out.startswith("2 match") and "3,Yoga mat" in out

out = server.find("shopping", where={"Item": "~KIM"})
assert "4,Kimchi flakes,H Mart,,TRUE" in out

# append must grow the grid (rowCount 6, 4 used, adding 3)
out = server.append("shopping", [{"Item": "4 glue sticks", "For": "school"},
                                 {"item": "Kleenex", "for": "school"},
                                 {"Item": "Sharpies", "Bought": False}])
print(out)
assert "rows 5-7" in out and vals()[6][0] == "Sharpies"
assert vals()[4] == ["4 glue sticks", "", "school", ""]

out = server.update("shopping", where={"For": "school"}, set={"Store": "Staples"})
print(out)
assert "rows 5, 6" in out and vals()[5][1] == "Staples"

out = server.delete("shopping", where={"Item": "Yoga mat"})
print(out)
assert [r[0] for r in vals()][:3] == ["Item", "Almond extract", "Kimchi flakes"]

print(server.cells("shopping", "'Off island'!A1:B2"))
print(server.cells("shopping", "'Off island'!D2", [["TRUE"]]))
assert vals()[1][3] == "TRUE"

for bad in (lambda: server.append("shopping", [{"Colour": "x"}]),
            lambda: server.find("shopping", tab="Nope")):
    try:
        bad()
        raise AssertionError("expected error")
    except ToolError as e:
        print("error ok:", e)

# ---- docs
server._aliases = lambda: {"shopping": {"id": SID}, "plan": {"id": DID, "notes": "Farm to-dos"}}
out = server.doc_read("plan")
print(out, "\n---")
assert out.startswith("DOC: Farm plan\nNOTES: Farm to-dos")
assert "# Chores\n- Feed hens\n- Water beds" in out and "# Budget" in out

out = server.doc_read(f"https://docs.google.com/document/d/{DID}/edit", section="~chore")
assert "Feed hens" in out and "Budget" not in out

# after a list item: joins the list; emoji before it must not skew indices
print(server.doc_edit("plan", insert="Fix fence", after="~water beds"))
assert [p["text"] for p in docs.paras][2:5] == ["Feed hens", "Water beds \U0001F331", "Fix fence"]
assert docs.paras[4]["bullet"] == 0

# after a heading: normal text, heading itself untouched
print(server.doc_edit("plan", insert="Hay: $400\nFeed: $120", after="budget"))
b = docs.paras.index(next(p for p in docs.paras if p["text"] == "Budget"))
assert [(p["text"], p["style"]) for p in docs.paras[b:b + 3]] == [
    ("Budget", "HEADING_1"), ("Hay: $400", "NORMAL_TEXT"), ("Feed: $120", "NORMAL_TEXT")]

# at the end, after the last heading
print(server.doc_edit("plan", insert="Plant garlic"))
assert (docs.paras[-1]["text"], docs.paras[-1]["style"]) == ("Plant garlic", "NORMAL_TEXT")
assert docs.paras[-2]["style"] == "HEADING_1"

out = server.doc_edit("plan", replace={"hay": "straw", "nothing here": "x"})
print(out)
assert "'hay' x3" in out and "'nothing here' x0" in out and "Spend less on straw. straw is dear." in server.doc_read("plan")

docs.paras.append({"text": "ok " * 30, "style": "NORMAL_TEXT", "bullet": None})
n = len(docs.calls)
out = server.doc_edit("plan", replace={"ok": "fine"})
assert "pass all=True" in out and not any(c[0] == "POST" for c in docs.calls[n:])

for bad in (lambda: server.doc_edit("plan", insert="x", after="~e"),
            lambda: server.doc_edit("plan", insert="x", after="Nope"),
            lambda: server.doc_edit("plan"),
            lambda: server.doc_read("plan", section="Nope"),
            lambda: server.doc_read("plan", tab="Nope")):
    try:
        bad()
        raise AssertionError("expected error")
    except ToolError as e:
        print("error ok:", e)

# ---- doc tables
def cell(*lines):
    return [{"text": t} for t in lines]


docs = FakeDocs([
    {"text": "Rules", "style": "TITLE", "bullet": None},
    {"text": "Tenant Directory \u2013 house", "style": "HEADING_2", "bullet": None},
    {"table": [[cell("Room"), cell("Tenant name"), cell("Expected rent")],
               [cell("1"), cell("Ana \U0001F3E0"), cell("")],
               [cell(""), cell(""), cell("")],
               [cell(""), cell(""), cell("")]]},
    {"text": "", "style": "NORMAL_TEXT", "bullet": None},
    {"text": "Pets", "style": "HEADING_2", "bullet": None},
    {"table": [[cell("Pet"), cell("Owner")], [cell("Rex"), cell("Ana")]]},
    {"text": "Run Notes", "style": "HEADING_2", "bullet": None},
])
rows_of = lambda k: [[" ".join(p["text"] for p in c) for c in r] for r in docs.paras[k]["table"]]

# fill an empty cell on a matched row; emoji before it must not skew indices
print(server.doc_edit("plan", table="~tenant directory", where={"Tenant name": "~ana"},
                      set={"Expected rent": "$900"}))
assert rows_of(2)[1] == ["1", "Ana \U0001F3E0", "$900"]

# rows fill the empty rows first, then add rows at the bottom
out = server.doc_edit("plan", table="Expected rent", rows=[
    {"Room": "2", "Tenant name": "Bo", "Expected rent": "$800"},
    {"Room": "3", "Tenant name": "Cy"},
    {"Room": "4", "Tenant name": "Di\nand Ed", "Expected rent": "$1,000"}])
print(out)
assert rows_of(2)[1:] == [["1", "Ana \U0001F3E0", "$900"], ["2", "Bo", "$800"], ["3", "Cy", ""],
                          ["4", "Di and Ed", "$1,000"]], rows_of(2)
assert "row(s) 2, 3, 4" in out
assert rows_of(5) == [["Pet", "Owner"], ["Rex", "Ana"]]  # other table untouched
assert docs.paras[6]["text"] == "Run Notes"

# set replaces existing text, and "" clears a cell
server.doc_edit("plan", table="~tenant", where={"Room": "2"}, set={"Expected rent": "$850", "Room": ""})
assert rows_of(2)[2] == ["", "Bo", "$850"]
out = server.doc_read("plan", section="~tenant")
assert "| Room | Tenant name | Expected rent |" in out and "|  | Bo | $850 |" in out, out

n = len(docs.calls)
assert "0 table rows matched" in server.doc_edit("plan", table="Pet", where={"Pet": "Cat"}, set={"Owner": "x"})
assert not any(c[0] == "POST" for c in docs.calls[n:])

for bad in (lambda: server.doc_edit("plan", where={"Pet": "Rex"}, set={"Owner": "x"}),  # 2 tables
            lambda: server.doc_edit("plan", table="Nope", rows=[{"Pet": "x"}]),
            lambda: server.doc_edit("plan", table="Pet", rows=[{"Colour": "x"}]),
            lambda: server.doc_edit("plan", table="Pet", set={"Owner": "x"})):
    n = len(docs.calls)
    try:
        bad()
        raise AssertionError("expected error")
    except ToolError as e:
        print("error ok:", e)
    assert not any(c[0] == "POST" for c in docs.calls[n:])

# bare URLs become links (after an emoji, in a table cell too); linked ones are left alone
out = server.doc_edit("plan", insert="Unsubscribe \U0001F4E7: https://ex.com/u?id=1&t=2. Or https://mail.google.com/mail/u/0/#all/19f",
                      after="Run Notes")
print(out)
assert "linked 2 URL(s)" in out
p = next(p for p in docs.paras if p.get("text", "").startswith("Unsubscribe"))
got = [(p["text"].encode("utf-16-le")[a * 2:b * 2].decode("utf-16-le"), u) for a, b, u in p["links"]]
assert got == [("https://ex.com/u?id=1&t=2", "https://ex.com/u?id=1&t=2"),
               ("https://mail.google.com/mail/u/0/#all/19f", "https://mail.google.com/mail/u/0/#all/19f")], got
out = server.doc_edit("plan", table="Pet", where={"Pet": "Rex"}, set={"Owner": "see http://ana.example"})
assert "linked 1 URL(s)" in out, out
n = len(docs.calls)
assert "linked" not in server.doc_edit("plan", replace={"Feed hens": "Feed the hens"})
assert len(docs.calls) - n == 3  # GET, replace, GET: nothing left to link

# [words](url) becomes the linked words; doc_read shows them back that way; replace matches them
out = server.doc_edit("plan", insert="Walrus \U0001F4E7: [UNSUBSCRIBE](https://ex.com/u?a=1) or [EMAIL](https://mail.google.com/#all/1a2) https://bare.example",
                      after="Run Notes")
print(out)
assert "linked 2 word(s)" in out and "linked 1 URL(s)" in out, out
p = next(p for p in docs.paras if p.get("text", "").startswith("Walrus"))
assert p["text"] == "Walrus \U0001F4E7: UNSUBSCRIBE or EMAIL https://bare.example", p["text"]
got = sorted((p["text"].encode("utf-16-le")[a * 2:b * 2].decode("utf-16-le"), u) for a, b, u in p["links"])
assert got == [("EMAIL", "https://mail.google.com/#all/1a2"), ("UNSUBSCRIBE", "https://ex.com/u?a=1"),
               ("https://bare.example", "https://bare.example")], got
shown = server.doc_read("plan", section="Run Notes")
assert "Walrus \U0001F4E7: [UNSUBSCRIBE](https://ex.com/u?a=1) or [EMAIL](https://mail.google.com/#all/1a2) https://bare.example" in shown, shown
out = server.doc_edit("plan", replace={"Walrus \U0001F4E7: [UNSUBSCRIBE](https://ex.com/u?a=1) or": "Walrus:"})
assert "x1" in out, out

assert server._col(0) == "A" and server._col(25) == "Z" and server._col(26) == "AA"
assert server._q("Dan's list") == "'Dan''s list'"
print("\nALL TESTS PASSED")
