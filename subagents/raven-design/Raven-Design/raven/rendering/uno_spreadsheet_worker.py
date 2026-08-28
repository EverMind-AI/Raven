"""LibreOffice UNO worker for deterministic spreadsheet page setup."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.uno import Exception as UnoException


def _property(name: str, value: Any) -> PropertyValue:
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def _column_name(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _range_name(address: Any) -> str:
    start = f"{_column_name(address.StartColumn)}{address.StartRow + 1}"
    end = f"{_column_name(address.EndColumn)}{address.EndRow + 1}"
    return start if start == end else f"{start}:{end}"


def _get_properties(target: Any, names: tuple[str, ...]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in names:
        try:
            value = getattr(target, name)
        except (AttributeError, UnoException):
            value = None
            available = False
        else:
            available = True
        if not available:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            values[name] = value
    return values


def _set_properties(target: Any, values: dict[str, Any]) -> None:
    for name, value in values.items():
        with suppress(AttributeError, UnoException):
            setattr(target, name, value)


def _connect(port: int, timeout_seconds: float) -> tuple[Any, Any]:
    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver",
        local_context,
    )
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            remote_context = resolver.resolve(f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")
            desktop = remote_context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop",
                remote_context,
            )
            return remote_context, desktop
        except UnoException as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError("Timed out connecting to LibreOffice.") from last_error


def _sheet_metadata(sheet: Any, index: int) -> tuple[dict[str, Any], Any]:
    cursor = sheet.createCursor()
    cursor.gotoEndOfUsedArea(True)
    address = cursor.RangeAddress
    used_range = sheet.getCellRangeByPosition(
        address.StartColumn,
        address.StartRow,
        address.EndColumn,
        address.EndRow,
    )
    columns = []
    hidden_columns = 0
    last_visible_column = address.StartColumn
    for column_index in range(address.StartColumn, address.EndColumn + 1):
        column = sheet.Columns.getByIndex(column_index)
        visible = bool(column.IsVisible)
        if not visible:
            hidden_columns += 1
        else:
            last_visible_column = column_index
        columns.append(
            {
                "index": column_index,
                "name": _column_name(column_index),
                "width_100th_mm": int(column.Width),
                "visible": visible,
                "optimal_width": bool(column.OptimalWidth),
            }
        )
    hidden_rows = 0
    for row_index in range(address.StartRow, address.EndRow + 1):
        if not sheet.Rows.getByIndex(row_index).IsVisible:
            hidden_rows += 1
    print_areas = [_range_name(item) for item in sheet.getPrintAreas()]
    return (
        {
            "index": index,
            "name": sheet.Name,
            "visible": bool(sheet.IsVisible),
            "used_range": _range_name(address),
            "used_columns": address.EndColumn - address.StartColumn + 1,
            "used_rows": address.EndRow - address.StartRow + 1,
            "used_width_100th_mm": int(used_range.Size.Width),
            "used_height_100th_mm": int(used_range.Size.Height),
            "hidden_column_count": hidden_columns,
            "last_visible_column_index": last_visible_column,
            "hidden_row_count": hidden_rows,
            "column_widths": columns,
            "print_areas": print_areas,
            "page_style": sheet.PageStyle,
        },
        address,
    )


def _export_pdf(document: Any, target: Path) -> None:
    document.storeToURL(
        uno.systemPathToFileUrl(str(target.resolve())),
        (
            _property("FilterName", "calc_pdf_Export"),
            _property("Overwrite", True),
        ),
    )


def _capture_root(target: Path, display: str, executable: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            executable,
            "-display",
            display,
            "-window",
            "root",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError(f"X11 capture failed: {(result.stderr or result.stdout).strip()}")


def _visible_range(controller: Any) -> dict[str, int]:
    address = controller.getVisibleRange()
    return {
        "start_column": int(address.StartColumn),
        "start_row": int(address.StartRow),
        "end_column": int(address.EndColumn),
        "end_row": int(address.EndRow),
    }


def _capture_sheet_viewports(
    document: Any,
    sheet: Any,
    metadata: dict[str, Any],
    sheet_dir: Path,
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    controller = document.CurrentController
    window = controller.Frame.ContainerWindow
    window.setPosSize(
        0,
        0,
        int(request["viewport_width"]),
        int(request["viewport_height"]),
        15,
    )
    window.setVisible(True)
    controller.setActiveSheet(sheet)
    for name, value in (
        ("HasColumnRowHeaders", True),
        ("HasSheetTabs", True),
        ("ShowGrid", True),
    ):
        with suppress(AttributeError, UnoException):
            setattr(controller, name, value)
    width = max(1, int(metadata["used_width_100th_mm"]))
    zoom = max(65, min(100, round(4_000_000 / width)))
    controller.ZoomType = 0
    controller.ZoomValue = zoom
    start_column = 0
    start_row = 0
    controller.select(sheet.getCellByPosition(start_column, start_row))
    controller.setFirstVisibleColumn(start_column)
    controller.setFirstVisibleRow(start_row)
    time.sleep(0.6)
    first_visible = _visible_range(controller)
    visible_columns = first_visible["end_column"] - first_visible["start_column"] + 1
    visible_rows = first_visible["end_row"] - first_visible["start_row"] + 1
    if visible_columns < 4:
        visible_columns = min(12, int(metadata["used_columns"]))
    if visible_rows < 10:
        visible_rows = min(40, int(metadata["used_rows"]))
    remaining_columns = int(metadata["last_visible_column_index"]) - first_visible["end_column"]
    last_column = (
        max(
            0,
            int(metadata["last_visible_column_index"]) - visible_columns + 1,
        )
        if remaining_columns > 2
        else 0
    )
    middle_row = max(0, (metadata["used_rows"] - visible_rows) // 2)
    last_row = max(0, metadata["used_rows"] - visible_rows)
    anchors = [(0, 0)]
    if last_column:
        anchors.append((last_column, 0))
    if middle_row:
        anchors.append((0, middle_row))
    if last_row:
        anchors.append((last_column, last_row))
    max_viewports = max(1, int(request["max_viewports"]))
    unique_anchors = list(dict.fromkeys(anchors))[:max_viewports]
    viewports = []
    display = request["display"]
    for column, row in unique_anchors:
        controller.select(sheet.getCellByPosition(column, row))
        controller.setFirstVisibleColumn(column)
        controller.setFirstVisibleRow(row)
        time.sleep(0.35)
        controller_visible_range = _visible_range(controller)
        target = sheet_dir / "viewports" / f"viewport-r{row + 1:05d}-c{column + 1:04d}.png"
        _capture_root(target, display, request["capture_path"])
        viewports.append(
            {
                "path": str(target),
                "zoom_percent": int(controller.ZoomValue),
                "anchor_row": row,
                "anchor_column": column,
                "controller_visible_range": controller_visible_range,
            }
        )
    return viewports


def _render_sheet(
    document: Any,
    sheets: Any,
    sheet: Any,
    address: Any,
    sheet_dir: Path,
) -> dict[str, Any]:
    for index in range(sheets.Count):
        candidate = sheets.getByIndex(index)
        candidate.IsVisible = candidate.Name == sheet.Name
    document.CurrentController.setActiveSheet(sheet)
    sheet.setPrintAreas((address,))
    page_style = document.StyleFamilies.getByName("PageStyles").getByName(sheet.PageStyle)
    property_names = (
        "ScaleToPages",
        "ScaleToPagesX",
        "ScaleToPagesY",
        "PageScale",
        "IsLandscape",
        "Width",
        "Height",
        "LeftMargin",
        "RightMargin",
        "TopMargin",
        "BottomMargin",
    )
    original = _get_properties(page_style, property_names)
    sheet_dir.mkdir(parents=True, exist_ok=True)
    used_range = sheet.getCellRangeByPosition(
        address.StartColumn,
        address.StartRow,
        address.EndColumn,
        address.EndRow,
    )
    width = int(used_range.Size.Width)
    height = int(used_range.Size.Height)
    landscape = width > height * 1.25
    short_edge = min(int(original.get("Width", 21000)), int(original.get("Height", 29700)))
    long_edge = max(int(original.get("Width", 21000)), int(original.get("Height", 29700)))
    normalized = {
        "IsLandscape": landscape,
        "Width": long_edge if landscape else short_edge,
        "Height": short_edge if landscape else long_edge,
        "LeftMargin": 500,
        "RightMargin": 500,
        "TopMargin": 500,
        "BottomMargin": 500,
    }
    try:
        _set_properties(page_style, normalized)
        _set_properties(
            page_style,
            {
                "ScaleToPagesX": 0,
                "ScaleToPagesY": 0,
                "ScaleToPages": 1,
            },
        )
        overview_pdf = sheet_dir / "overview.pdf"
        _export_pdf(document, overview_pdf)
        _set_properties(
            page_style,
            {
                "ScaleToPages": 0,
                "ScaleToPagesX": 1,
                "ScaleToPagesY": 0,
            },
        )
        pages_pdf = sheet_dir / "pages.pdf"
        _export_pdf(document, pages_pdf)
    finally:
        _set_properties(page_style, original)
    return {
        "overview_pdf": str(overview_pdf),
        "pages_pdf": str(pages_pdf),
        "normalized_orientation": "landscape" if landscape else "portrait",
    }


def run(request_path: Path) -> dict[str, Any]:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    source = Path(request["source"])
    output_dir = Path(request["output_dir"])
    port = int(request["port"])
    _, desktop = _connect(port, float(request.get("connect_timeout_seconds", 20)))
    document = None
    try:
        document = desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(str(source.resolve())),
            "_blank",
            0,
            (
                _property("Hidden", not bool(request.get("capture_viewports"))),
                _property("ReadOnly", False),
                _property("UpdateDocMode", 0),
                _property("MacroExecutionMode", 0),
            ),
        )
        if document is None:
            raise RuntimeError("LibreOffice could not open the spreadsheet.")
        sheets = document.Sheets
        active_name = document.CurrentController.ActiveSheet.Name
        source_visibility = [bool(sheets.getByIndex(index).IsVisible) for index in range(sheets.Count)]
        source_print_areas = [tuple(sheets.getByIndex(index).getPrintAreas()) for index in range(sheets.Count)]
        result: dict[str, Any] = {
            "active_sheet": active_name,
            "sheet_count": sheets.Count,
            "sheets": [],
        }
        sheet_data = []
        for index in range(sheets.Count):
            sheet = sheets.getByIndex(index)
            metadata, address = _sheet_metadata(sheet, index)
            sheet_data.append((sheet, metadata, address))
        if request.get("capture_viewports"):
            for sheet, metadata, _ in sheet_data:
                if not metadata["visible"]:
                    continue
                sheet_dir = output_dir / f"sheet-{metadata['index'] + 1:04d}"
                try:
                    metadata["viewports"] = _capture_sheet_viewports(
                        document,
                        sheet,
                        metadata,
                        sheet_dir,
                        request,
                    )
                except Exception as exc:  # noqa: BLE001
                    metadata["viewport_error"] = f"{type(exc).__name__}: {exc}"
        for sheet, metadata, address in sheet_data:
            metadata["active"] = sheet.Name == active_name
            metadata["rendered"] = False
            if metadata["visible"]:
                sheet_dir = output_dir / (f"sheet-{metadata['index'] + 1:04d}")
                try:
                    metadata["artifacts"] = _render_sheet(
                        document,
                        sheets,
                        sheet,
                        address,
                        sheet_dir,
                    )
                    metadata["rendered"] = True
                except Exception as exc:  # noqa: BLE001
                    metadata["render_error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    for index, visible in enumerate(source_visibility):
                        sheets.getByIndex(index).IsVisible = visible
                    for index, print_areas in enumerate(source_print_areas):
                        sheets.getByIndex(index).setPrintAreas(print_areas)
            result["sheets"].append(metadata)
        return result
    finally:
        if document is not None:
            document.close(True)
        with suppress(UnoException):
            desktop.terminate()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    result = run(args.request)
    args.result.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
