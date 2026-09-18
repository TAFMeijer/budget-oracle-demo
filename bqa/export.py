"""Excel export with sensible number formats."""
from __future__ import annotations

import io

import pandas as pd


def to_xlsx(df: pd.DataFrame, sheet: str = "Results") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet)
        ws = writer.sheets[sheet]
        for col_name in df.select_dtypes(include="number").columns:
            idx = df.columns.get_loc(col_name) + 1
            fmt = "0.0%" if col_name == "% of total" else "#,##0"
            for row in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                for cell in row:
                    cell.number_format = fmt
        for i, col_name in enumerate(df.columns, start=1):
            width = max(12, min(48, int(df[col_name].astype(str).str.len().max() if len(df) else 12) + 2))
            ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    return buf.getvalue()
