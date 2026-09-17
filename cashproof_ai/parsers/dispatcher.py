"""
cashproof_ai/parsers/dispatcher.py

Routes an uploaded file to the correct parser and returns
(account_meta, transactions) using the CashProof AI schema.
"""

import os
from .pdf_parser import PDFStatementParser
from .csv_excel_parser import CSVExcelParser


def dispatch(file_path: str) -> tuple[dict, list[dict]]:
    """
    Dispatch *file_path* to the appropriate parser.

    Returns:
        account_meta  – dict with header fields (§2)
        transactions  – list of transaction dicts (§3 schema)
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.pdf':
        parser = PDFStatementParser(file_path)
    elif ext in ('.csv', '.xlsx', '.xls'):
        parser = CSVExcelParser(file_path)
    else:
        raise ValueError(f'Unsupported file format: {ext!r}. Accepted: PDF, CSV, XLSX, XLS.')

    return parser.parse()
