# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2,<3", "google-auth>=2.20", "requests>=2.31"]
# ///
"""Offline tests: runs every tool against an in-memory fake of the Sheets API.
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

    def __call__(self, method, path, **kw):
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

assert server._col(0) == "A" and server._col(25) == "Z" and server._col(26) == "AA"
assert server._q("Dan's list") == "'Dan''s list'"
print("\nALL TESTS PASSED")
