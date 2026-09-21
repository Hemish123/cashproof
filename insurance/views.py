import os
import re
import json

import pandas as pd
import openai
from django.shortcuts import render, redirect, get_object_or_404

from .models import InsuranceAnalysis

MAX_CATEGORIES = 12       # a column with more unique values than this is not treated as a category
MAX_PREVIEW_ROWS = 10
VALID_CHART_TYPES = {"bar", "pie", "line", "doughnut"}


# ---------------------------------------------------------------------------
# 1. Read + profile the data (exact numbers over ALL rows, computed by pandas)
# ---------------------------------------------------------------------------
def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(how="all").dropna(axis=1, how="all").copy()
    df.columns = [str(c).strip() for c in df.columns]
    # Dates that were typed as text in Excel -> real dates
    for col in df.columns:
        if df[col].dtype == object and "date" in col.lower():
            parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
            if parsed.notna().mean() > 0.8:
                df[col] = parsed
    return df


def _is_identifier(s: pd.Series, n_rows: int, name: str = "") -> bool:
    """ID / name-like column: (almost) every value is different."""
    if n_rows < 20 or len(s) == 0:
        return False
    if pd.api.types.is_datetime64_any_dtype(s):
        return False
    ratio = s.nunique() / n_rows
    if pd.api.types.is_numeric_dtype(s):
        # amounts and premiums are often unique too, so only flag numbers that look like IDs by name
        looks_like_id = re.search(r"(^|[\s_])(id|no|number|code)($|[\s_])", name.lower()) is not None
        return looks_like_id and ratio > 0.98
    return ratio > 0.9


def _round(x):
    return round(float(x), 2)


def build_profile(df: pd.DataFrame) -> dict:
    n = len(df)
    profile = {"total_rows": n, "total_columns": len(df.columns), "columns": []}
    cat_cols, num_cols, date_cols = [], [], []

    for col in df.columns:
        s = df[col]
        nn = s.dropna()
        info = {
            "name": col,
            "dtype": str(s.dtype),
            "missing": int(s.isna().sum()),
            "unique": int(nn.nunique()),
        }
        ident = _is_identifier(nn, n, col)
        if ident:
            info["likely_identifier"] = True

        is_bool = pd.api.types.is_bool_dtype(s)
        if pd.api.types.is_numeric_dtype(s) and not is_bool:
            if len(nn):
                info["stats"] = {
                    "min": _round(nn.min()), "max": _round(nn.max()),
                    "mean": _round(nn.mean()), "median": _round(nn.median()),
                    "sum": _round(nn.sum()),
                }
                if info["unique"] <= 8:
                    info["top_values"] = {str(k): int(v) for k, v in nn.value_counts().sort_index().items()}
                else:
                    try:
                        binned = pd.cut(nn, bins=min(6, info["unique"]), duplicates="drop", precision=0)
                        info["distribution"] = {str(k): int(v) for k, v in binned.value_counts().sort_index().items()}
                    except Exception:
                        pass
            if not ident:
                num_cols.append(col)
        elif pd.api.types.is_datetime64_any_dtype(s):
            if len(nn):
                info["range"] = {"from": str(nn.min().date()), "to": str(nn.max().date())}
            date_cols.append(col)
        else:
            info["top_values"] = {str(k): int(v) for k, v in nn.astype(str).value_counts().head(MAX_CATEGORIES).items()}
            if not ident and 1 < info["unique"] <= MAX_CATEGORIES:
                cat_cols.append(col)
        profile["columns"].append(info)

    # Aggregates: numeric measures broken down by each category (e.g. premium by policy type)
    breakdowns = []
    for cat in cat_cols[:6]:
        measures = {}
        for num in num_cols[:3]:
            g = df.groupby(cat)[num].agg(["count", "mean", "sum"]).round(2)
            measures[num] = {str(k): {"count": int(r["count"]), "mean": float(r["mean"]), "sum": float(r["sum"])}
                             for k, r in g.iterrows()}
        if measures:
            breakdowns.append({"by": cat, "measures": measures})
    if breakdowns:
        profile["breakdowns"] = breakdowns

    # Trend over time for the first date column
    if date_cols:
        d = df[date_cols[0]].dropna()
        if len(d):
            span_days = (d.max() - d.min()).days
            freq = "M" if span_days > 62 else "D"
            counts = d.dt.to_period(freq).value_counts().sort_index().tail(36)
            profile["time_series"] = {"column": date_cols[0], "granularity": "month" if freq == "M" else "day",
                                      "counts": {str(k): int(v) for k, v in counts.items()}}
    return profile


# ---------------------------------------------------------------------------
# 2. Prompt
# ---------------------------------------------------------------------------
PROMPT_TEMPLATE = """
You are a senior data analyst. A user has uploaded an Excel file. It may be about
insurance, sales, HR, finance, healthcare, or anything else, so first work out
what the data represents and then analyze it accordingly.

## DATA PROVIDED
1. DATA PROFILE: exact statistics calculated over ALL rows. This is your source of truth.
   It contains per-column stats, value counts, numeric distributions, "breakdowns"
   (count / mean / sum of numeric columns per category) and an optional "time_series".
__PROFILE__

2. SAMPLE ROWS (first 15 rows, CSV): use only to understand the meaning of columns.
__SAMPLE__

## RULES
- Use ONLY the numbers in the DATA PROFILE. Never guess, extrapolate from the sample, or invent values.
- If a column is missing or the data cannot answer something, skip that analysis instead of making it up.
- Currency: if the column names or values suggest Indian data (INR, Rs, ₹, Indian cities), format money
  with ₹ and Indian digit grouping (e.g. ₹12,50,000, or ₹12.5 L / ₹1.2 Cr for large values). Otherwise use plain numbers.
- Group continuous numbers (amounts, premiums, ages) into readable ranges, e.g. "₹1L - ₹5L". Never make a label for every unique value.
- Show at most 10 categories per chart. Group the remainder as "Others".
- Never chart columns marked "likely_identifier" (IDs, names). Use them only for counting records.
- Keep insights specific and quantified (with numbers and percentages), never generic.

## WHAT TO ANALYZE
1. Identify the domain and the main entity (e.g. "Insurance policies").
2. If the data is INSURANCE related, focus on:
   - The distribution of records by insurance amount (sum assured / coverage), in ranges, with the count of records per range
   - Breakdown by policy type, status, city/state, or sales channel
   - Premium analysis (average, total, premium by policy type)
   - Claims analysis if present (claim ratio, approved vs rejected, average claim amount)
   - Trend over time if a date column exists
3. If the data is NOT insurance, pick the 3-5 most meaningful dimensions and measures
   (a category column plus a numeric or count measure) and analyze those.
4. Find notable patterns: the largest segment, outliers, imbalances, and risks.
5. Report data quality issues: missing values, suspicious columns, or anything limiting the analysis.

## OUTPUT
Return ONLY valid JSON, with no markdown fences and no commentary, in exactly this structure:

{
  "summary": {
    "domain": "detected domain, e.g. Insurance policies",
    "description": "1-2 sentence description of what this dataset contains"
  },
  "metrics": [
    {"label": "Total Records", "value": "300"}
  ],
  "charts": [
    {
      "title": "Number of Policies by Sum Assured",
      "type": "bar",
      "labels": ["₹1L - ₹5L", "₹5L - ₹10L"],
      "datasets": [{"label": "Policies", "data": [120, 80]}]
    }
  ],
  "insights": ["Specific, quantified finding about the data"],
  "dataQuality": ["Any issues found, or an empty array if none"]
}

Constraints:
- "metrics": 6 to 8 items. Every value is a string.
- "charts": 3 to 5 charts. "type" must be one of "bar", "pie", "line", or "doughnut"
  (use "line" only for time-based data, "pie"/"doughnut" only for 6 or fewer categories).
  Each chart's labels and data arrays must be the same length. Data must be numbers, not strings.
- The FIRST chart must answer: "how many records fall in each insurance/coverage amount range"
  (or the closest equivalent if the data is not insurance).
- "insights": 4 to 6 items, each one sentence.
"""


def build_prompt(profile: dict, sample_csv: str) -> str:
    return (PROMPT_TEMPLATE
            .replace("__PROFILE__", json.dumps(profile, ensure_ascii=False, default=str))
            .replace("__SAMPLE__", sample_csv))


# ---------------------------------------------------------------------------
# 3. Parse + validate the AI answer (never trust it blindly)
# ---------------------------------------------------------------------------
def parse_ai_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    return json.loads(text)


def _num(x):
    v = float(x)
    return int(v) if v.is_integer() else round(v, 2)


def clean_charts(charts) -> list:
    out = []
    if not isinstance(charts, list):
        return out
    for c in charts:
        try:
            labels = [str(x) for x in c["labels"]]
            datasets = []
            for ds in c["datasets"]:
                data = [_num(x) for x in ds["data"]]
                if len(data) != len(labels):
                    raise ValueError("length mismatch")
                datasets.append({"label": str(ds.get("label", "Value")), "data": data})
            if not labels or not datasets:
                continue
            ctype = c.get("type") if c.get("type") in VALID_CHART_TYPES else "bar"
            out.append({"title": str(c.get("title", "Chart")), "type": ctype,
                        "labels": labels, "datasets": datasets})
        except (KeyError, TypeError, ValueError):
            continue
    return out[:5]


def clean_metrics(metrics) -> list:
    out = []
    if isinstance(metrics, list):
        for m in metrics:
            if isinstance(m, dict) and "label" in m and "value" in m:
                out.append({"label": str(m["label"]), "value": str(m["value"])})
    return out[:8]


def clean_str_list(items) -> list:
    return [str(i) for i in items if str(i).strip()] if isinstance(items, list) else []


def fallback_charts(profile: dict) -> list:
    """If the AI returned no usable chart, still show something real."""
    for col in profile["columns"]:
        tv = col.get("top_values")
        if tv and not col.get("likely_identifier"):
            return [{"title": f"Records by {col['name']}", "type": "bar", "labels": list(tv.keys()),
                     "datasets": [{"label": "Records", "data": list(tv.values())}]}]
    return []


# ---------------------------------------------------------------------------
# 4. Views
# ---------------------------------------------------------------------------
def upload_file(request):
    if request.method != 'POST':
        return render(request, 'insurance/upload.html')

    excel_file = request.FILES.get('excel_file')
    if not excel_file:
        return render(request, 'insurance/upload.html', {'error': 'No file was uploaded.'})

    try:
        df = _clean_df(pd.read_excel(excel_file))
        if df.empty:
            raise ValueError("The first sheet of this file has no data.")

        profile = build_profile(df)
        prompt = build_prompt(profile, df.head(15).to_csv(index=False))

        client = openai.AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        )
        response = client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            response_format={"type": "json_object"},   # remove this line if your deployment rejects it
        )
        ai = parse_ai_json(response.choices[0].message.content)

        summary = ai.get("summary") if isinstance(ai.get("summary"), dict) else {}
        charts = clean_charts(ai.get("charts")) or fallback_charts(profile)

        # Small table + column overview shown on the dashboard
        preview_df = df.head(MAX_PREVIEW_ROWS).astype(object).where(df.head(MAX_PREVIEW_ROWS).notna(), "")
        extras = {
            "domain": str(summary.get("domain", "")),
            "description": str(summary.get("description", "")),
            "charts": charts,
            "dataQuality": clean_str_list(ai.get("dataQuality")),
            "total_rows": profile["total_rows"],
            "columns": [{"name": c["name"], "dtype": c["dtype"], "missing": c["missing"],
                         "unique": c["unique"],
                         "missing_pct": round(c["missing"] * 100 / max(profile["total_rows"], 1), 1)}
                        for c in profile["columns"]],
            "preview": {"columns": list(preview_df.columns),
                        "rows": [[str(v) for v in row] for row in preview_df.values.tolist()]},
        }

        analysis_obj = InsuranceAnalysis.objects.create(
            file_name=excel_file.name,
            summary=extras["description"][:250],      # short text kept in the original field
            insights=clean_str_list(ai.get("insights")),
            metrics=clean_metrics(ai.get("metrics")),
            chart_data=extras,                        # JSONField: charts, quality notes, preview, columns
        )
        return redirect('insurance_dashboard', analysis_id=analysis_obj.id)

    except Exception as e:
        return render(request, 'insurance/upload.html', {'error': f"Could not analyze this file: {e}"})


def dashboard(request, analysis_id):
    obj = get_object_or_404(InsuranceAnalysis, id=analysis_id)
    data = obj.chart_data or {}
    if isinstance(data, str):
        data = json.loads(data)

    # Old records saved with the single-chart format still open
    if "labels" in data and "datasets" in data:
        data = {"charts": [{"title": "Interest by Insurance Amount", "type": "bar",
                            "labels": data["labels"], "datasets": data["datasets"]}]}

    analysis = {
        "summary": data.get("description") or obj.summary,
        "domain": data.get("domain", ""),
        "metrics": obj.metrics or [],
        "insights": obj.insights or [],
        "charts": data.get("charts", []),
        "dataQuality": data.get("dataQuality", []),
        "columns": data.get("columns", []),
        "preview": data.get("preview"),
        "totalRows": data.get("total_rows"),
    }
    return render(request, 'insurance/dashboard.html', {
        'analysis': analysis,
        'file_name': obj.file_name,
        'uploaded_at': obj.uploaded_at,
    })