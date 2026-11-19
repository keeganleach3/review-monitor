"""The Google Sheets backend builds correct API calls (checked against a fake HTTP session)."""
from review_monitor.sheet.backends import GoogleSheets, TabStyle


class FakeResp:
    def __init__(self, data, status=200):
        import json
        self.status_code, self.text, self._d = status, json.dumps(data), data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self):
        self.calls, self.headers = [], {}
        self.sheets = [{"properties": {"title": "Sheet1", "sheetId": 0}}]

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if method == "GET" and url.endswith("/abc"):
            return FakeResp({"sheets": self.sheets})
        if url.endswith(":batchUpdate"):
            for r in kw["json"]["requests"]:
                if "addSheet" in r:
                    self.sheets.append({"properties": {"title": r["addSheet"]["properties"]["title"], "sheetId": len(self.sheets) + 10}})
                if "deleteSheet" in r:
                    self.sheets = [s for s in self.sheets if s["properties"]["sheetId"] != r["deleteSheet"]["sheetId"]]
        return FakeResp({})


def test_google_backend_requests():
    s = FakeSession()
    g = GoogleSheets("abc", session=s)
    g.reset_tab("Dashboard", index=0, style=TabStyle(hide_grid=True, tab_color="#1F3A5F"))
    g.write("Dashboard", 2, 3, [["=SUM(A:A)"]])
    g.style("Dashboard", TabStyle(freeze_rows=4, widths={2: 120},
                                  formats=[(1, 1, 1, 3, {"bold": True, "bg": "#1F3A5F", "wrap": "wrap", "numfmt": "0%"})],
                                  conditional=[(5, 6, 20, 6, "blocked", "#8A5A00", "#FFF4D6")]))
    add = next(c for c in s.calls if c[1].endswith(":batchUpdate") and "addSheet" in str(c[2]["json"]))
    props = add[2]["json"]["requests"][-1]["addSheet"]["properties"]
    assert props["title"] == "Dashboard" and props["index"] == 0 and props["gridProperties"]["hideGridlines"]
    put = next(c for c in s.calls if c[0] == "PUT")
    assert put[1].endswith("/values/%27Dashboard%27%21C2") and put[2]["params"]["valueInputOption"] == "USER_ENTERED"
    reqs = s.calls[-1][2]["json"]["requests"]
    kinds = [next(iter(r)) for r in reqs]
    assert kinds == ["updateSheetProperties", "updateDimensionProperties", "repeatCell", "addConditionalFormatRule"]
    cell = reqs[2]["repeatCell"]
    assert cell["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "PERCENT"
    assert "userEnteredFormat.wrapStrategy" in cell["fields"]


def test_scraped_data_is_written_raw_so_sheets_cannot_reinterpret_it():
    """USER_ENTERED would turn "2026-11-17" into a date serial on read-back and a review starting
    with "=" into a live formula. Data rows go in RAW; only our own formulas are USER_ENTERED."""
    s = FakeSession()
    g = GoogleSheets("abc", session=s)
    g.reset_tab("Run 2026-11-19 10.00.00 #2", index=2)  # index past the tab count is clamped; '#' is URL-quoted
    g.write("Run 2026-11-19 10.00.00 #2", 5, 1, [[1, "Airbnb", "2026-11-17", 5, 5, "Guest", "=IMPORTDATA(\"x\")"]], raw=True)
    add = next(c for c in s.calls if c[1].endswith(":batchUpdate"))
    assert add[2]["json"]["requests"][-1]["addSheet"]["properties"]["index"] == 1
    put = next(c for c in s.calls if c[0] == "PUT")
    assert put[2]["params"]["valueInputOption"] == "RAW"
    assert "#" not in put[1] and put[1].endswith("%232%27%21A5")
