import pandas as pd
import sys

try:
    file_path = r"d:\cashproof\QNB_KwikGoal_Cashflow_Report.xlsx"
    xls = pd.ExcelFile(file_path)
    print("Sheets:", xls.sheet_names)
    for sheet in xls.sheet_names:
        print(f"\n--- Sheet: {sheet} ---")
        df = pd.read_excel(xls, sheet)
        print("Columns:", df.columns.tolist())
        print("First 5 rows:")
        print(df.head(5).to_dict('records'))
except Exception as e:
    print(f"Error: {e}")
