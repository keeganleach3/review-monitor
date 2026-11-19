"""Two interchangeable spreadsheet backends behind one small interface.

GoogleSheets: the real output, via the Sheets REST API and a service account (no model involved).
Xlsx:         the same layout written to a local .xlsx, for offline runs, tests and visual checks.

Cell values: `write(..., raw=False)` lets the spreadsheet parse what it gets (formulas, numbers), which is
what our own formulas need. `raw=True` stores the text exactly as given, which scraped data needs: a guest
review that starts with "=" must stay text, and a date like "2026-11-17" must read back as that string,
not as a spreadsheet date serial.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import requests


def col_letter(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(row: int, col: int) -> str:
    return f"{col_letter(col)}{row}"


def hex_rgb(h: str) -> dict:
    h = h.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


@dataclass
class TabStyle:
    freeze_rows: int = 0
    freeze_cols: int = 0
    widths: dict = field(default_factory=dict)        # 1-based column -> pixels
    formats: list = field(default_factory=list)       # (r1, c1, r2, c2, {bold,size,fg,bg,wrap,halign,valign,numfmt,italic})
    conditional: list = field(default_factory=list)   # (r1, c1, r2, c2, contains_text, fg, bg)
    tab_color: str | None = None
    hide_grid: bool = False


# ------------------------------------------------------------------------------------------------
class GoogleSheets:
    """Sheets API v4. Auth: GOOGLE_APPLICATION_CREDENTIALS -> service-account JSON; the spreadsheet must
    be shared with that service account's email (Editor)."""

    API = "https://sheets.googleapis.com/v4/spreadsheets"

    def __init__(self, spreadsheet_id: str, credentials_file: str | None = None, session=None):
        self.id = spreadsheet_id
        self.base = f"{self.API}/{spreadsheet_id}"
        self.session = session or requests.Session()
        if session is None:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                credentials_file or os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
                scopes=["https://www.googleapis.com/auth/spreadsheets"])
            creds.refresh(Request())
            self.session.headers["Authorization"] = f"Bearer {creds.token}"
        self._sheets: dict[str, int] | None = None

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.id}/edit"

    def _req(self, method: str, url: str, **kw):
        for attempt in range(4):  # Sheets API quota is per minute: back off on 429 / 5xx
            r = self.session.request(method, url, timeout=30, **kw)
            if r.status_code not in (429, 500, 502, 503) or attempt == 3:
                break
            time.sleep(2 ** attempt * 2)
        if r.status_code >= 400:
            raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {}

    def _batch(self, requests_: list) -> None:
        if requests_:
            self._req("POST", f"{self.base}:batchUpdate", json={"requests": requests_})

    def _load(self) -> dict[str, int]:
        if self._sheets is None:
            data = self._req("GET", self.base, params={"fields": "sheets.properties(sheetId,title)"})
            self._sheets = {s["properties"]["title"]: s["properties"]["sheetId"] for s in data.get("sheets", [])}
        return self._sheets

    def tab_names(self) -> list[str]:
        return list(self._load())

    def reset_tab(self, name: str, index: int | None = None, style: TabStyle | None = None) -> None:
        """Delete the tab if it exists and create it fresh (clean formats and rules)."""
        sheets = self._load()
        reqs = []
        placeholder = name in sheets and len(sheets) == 1  # a spreadsheet must keep one tab
        if name in sheets:
            if placeholder:
                reqs.append({"addSheet": {"properties": {"title": "_tmp"}}})
            reqs.append({"deleteSheet": {"sheetId": sheets[name]}})
        props = {"title": name, "gridProperties": {"hideGridlines": bool(style and style.hide_grid)}}
        if index is not None:  # the API rejects an index past the current tab count
            props["index"] = min(index, len(sheets) - (name in sheets))
        if style and style.tab_color:
            props["tabColorStyle"] = {"rgbColor": hex_rgb(style.tab_color)}
        reqs.append({"addSheet": {"properties": props}})
        self._batch(reqs)
        self._sheets = None
        if placeholder:  # remove only the placeholder this call added, never a user's own "_tmp" tab
            self._batch([{"deleteSheet": {"sheetId": self._load()["_tmp"]}}])
            self._sheets = None

    def _range(self, name: str, cells: str = "") -> str:
        return f"{self.base}/values/" + quote(f"'{name}'" + (f"!{cells}" if cells else ""), safe="")

    def read(self, name: str, cells: str = "", render: str = "UNFORMATTED_VALUE") -> list[list]:
        """`render`: UNFORMATTED_VALUE (raw values), FORMATTED_VALUE (as shown) or FORMULA."""
        if name not in self._load():
            return []
        return self._req("GET", self._range(name, cells), params={"valueRenderOption": render}).get("values", [])

    def write(self, name: str, row: int, col: int, values: list[list], raw: bool = False) -> None:
        self._req("PUT", self._range(name, a1(row, col)),
                  params={"valueInputOption": "RAW" if raw else "USER_ENTERED"}, json={"values": values})

    def style(self, name: str, st: TabStyle) -> None:
        sid = self._load()[name]
        reqs = [{"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {
            "frozenRowCount": st.freeze_rows, "frozenColumnCount": st.freeze_cols, "hideGridlines": st.hide_grid}},
            "fields": "gridProperties(frozenRowCount,frozenColumnCount,hideGridlines)"}}]
        for col, px in st.widths.items():
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": col - 1, "endIndex": col},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        for r1, c1, r2, c2, f in st.formats:
            if r2 < r1 or c2 < c1:
                continue
            fmt, fields = {}, []
            tf = {}
            if "bold" in f: tf["bold"] = f["bold"]
            if "italic" in f: tf["italic"] = f["italic"]
            if "size" in f: tf["fontSize"] = f["size"]
            if "fg" in f: tf["foregroundColorStyle"] = {"rgbColor": hex_rgb(f["fg"])}
            if tf:
                fmt["textFormat"] = tf
                fields += [f"userEnteredFormat.textFormat.{k}" for k in tf]
            if "bg" in f:
                fmt["backgroundColorStyle"] = {"rgbColor": hex_rgb(f["bg"])}
                fields.append("userEnteredFormat.backgroundColorStyle")
            if "wrap" in f:
                fmt["wrapStrategy"] = {"wrap": "WRAP", "clip": "CLIP"}[f["wrap"]]
                fields.append("userEnteredFormat.wrapStrategy")
            if "halign" in f:
                fmt["horizontalAlignment"] = f["halign"].upper()
                fields.append("userEnteredFormat.horizontalAlignment")
            if "valign" in f:
                fmt["verticalAlignment"] = {"top": "TOP", "middle": "MIDDLE"}[f["valign"]]
                fields.append("userEnteredFormat.verticalAlignment")
            if "numfmt" in f:
                kind = "PERCENT" if "%" in f["numfmt"] else "NUMBER"
                fmt["numberFormat"] = {"type": kind, "pattern": f["numfmt"]}
                fields.append("userEnteredFormat.numberFormat")
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2,
                          "startColumnIndex": c1 - 1, "endColumnIndex": c2},
                "cell": {"userEnteredFormat": fmt}, "fields": ",".join(fields)}})
        for i, (r1, c1, r2, c2, text, fg, bg) in enumerate(c for c in st.conditional if c[2] >= c[0]):
            reqs.append({"addConditionalFormatRule": {"index": i, "rule": {
                "ranges": [{"sheetId": sid, "startRowIndex": r1 - 1, "endRowIndex": r2,
                            "startColumnIndex": c1 - 1, "endColumnIndex": c2}],
                "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": text}]},
                                "format": {"textFormat": {"foregroundColorStyle": {"rgbColor": hex_rgb(fg)}, "bold": True},
                                           "backgroundColorStyle": {"rgbColor": hex_rgb(bg)}}}}}})
        self._batch(reqs)

    def delete_tab(self, name: str) -> None:
        sheets = self._load()
        if name in sheets:
            self._batch([{"deleteSheet": {"sheetId": sheets[name]}}])
            self._sheets = None

    def move(self, name: str, index: int) -> None:
        self._batch([{"updateSheetProperties": {"properties": {"sheetId": self._load()[name], "index": index},
                                                "fields": "index"}}])

    def save(self) -> None:
        pass


# ------------------------------------------------------------------------------------------------
class Xlsx:
    """Same interface, written to a local .xlsx (for offline runs, tests and rendering previews)."""

    def __init__(self, path: str | Path):
        from openpyxl import Workbook, load_workbook
        self.path = Path(path)
        if self.path.exists():
            self.wb = load_workbook(self.path)
        else:
            self.wb = Workbook()
            self.wb.active.title = "_tmp"

    @property
    def url(self) -> str:
        return str(self.path.resolve())

    def tab_names(self) -> list[str]:
        return [n for n in self.wb.sheetnames if n != "_tmp"]

    def reset_tab(self, name: str, index: int | None = None, style: TabStyle | None = None) -> None:
        if name in self.wb.sheetnames:
            del self.wb[name]
        ws = self.wb.create_sheet(name, index if index is not None else len(self.wb.sheetnames))
        if style and style.tab_color:
            ws.sheet_properties.tabColor = style.tab_color.lstrip("#")
        ws.sheet_view.showGridLines = not (style and style.hide_grid)
        if "_tmp" in self.wb.sheetnames and len(self.wb.sheetnames) > 1:
            del self.wb["_tmp"]

    def read(self, name: str) -> list[list]:
        if name not in self.wb.sheetnames:
            return []
        return [list(r) for r in self.wb[name].iter_rows(values_only=True)]

    def write(self, name: str, row: int, col: int, values: list[list], raw: bool = False) -> None:
        ws = self.wb[name]
        for i, r in enumerate(values):
            for j, v in enumerate(r):
                cell = ws.cell(row=row + i, column=col + j, value=v)
                if raw and isinstance(v, str):
                    cell.data_type = "s"  # text stays text, even when it starts with "="

    def style(self, name: str, st: TabStyle) -> None:
        from openpyxl.formatting.rule import FormulaRule
        from openpyxl.styles import Alignment, Font, PatternFill
        ws = self.wb[name]
        ws.page_setup.orientation = "landscape"          # for previews / printing: fit columns to one page wide
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        if st.freeze_rows or st.freeze_cols:
            ws.freeze_panes = a1(st.freeze_rows + 1, st.freeze_cols + 1)
        ws.sheet_view.showGridLines = not st.hide_grid
        for col, px in st.widths.items():
            ws.column_dimensions[col_letter(col)].width = round(px / 7, 1)
        for r1, c1, r2, c2, f in st.formats:
            if r2 < r1 or c2 < c1:
                continue
            for row in ws.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
                for cell in row:
                    if any(k in f for k in ("bold", "size", "fg", "italic")):
                        old = cell.font
                        cell.font = Font(bold=f.get("bold", old.bold), italic=f.get("italic", old.italic),
                                         size=f.get("size", old.size),
                                         color=f["fg"].lstrip("#") if "fg" in f else old.color)
                    if "bg" in f:
                        cell.fill = PatternFill("solid", fgColor=f["bg"].lstrip("#"))
                    if any(k in f for k in ("wrap", "halign", "valign")):
                        old = cell.alignment
                        cell.alignment = Alignment(
                            wrap_text=(f["wrap"] == "wrap") if "wrap" in f else old.wrap_text,
                            horizontal=f.get("halign", old.horizontal),
                            vertical={"top": "top", "middle": "center"}.get(f.get("valign"), old.vertical))
                    if "numfmt" in f:
                        cell.number_format = f["numfmt"]
        for r1, c1, r2, c2, text, fg, bg in st.conditional:
            if r2 < r1:
                continue
            rng = f"{a1(r1, c1)}:{a1(r2, c2)}"
            ws.conditional_formatting.add(rng, FormulaRule(
                formula=[f'ISNUMBER(SEARCH("{text}",{a1(r1, c1)}))'], stopIfTrue=True,
                font=Font(color=fg.lstrip("#"), bold=True), fill=PatternFill("solid", fgColor=bg.lstrip("#"))))

    def delete_tab(self, name: str) -> None:
        if name in self.wb.sheetnames:
            del self.wb[name]

    def move(self, name: str, index: int) -> None:
        ws = self.wb[name]
        self.wb.move_sheet(ws, offset=index - self.wb.sheetnames.index(name))

    def save(self) -> None:
        self.wb.save(self.path)
