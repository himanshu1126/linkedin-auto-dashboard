"""Excel / CSV import and message-template rendering.

Reads whatever spreadsheet the user uploads, without assuming fixed
column names -- the user maps their own columns to fields in the
Streamlit UI. Message columns may contain plain, already-personalized
text, or a template with {column_name} placeholders that get filled in
from the other columns in that same row.
"""
from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Any, Optional

import pandas as pd

MAX_MESSAGES = 5

# very loose check -- just enough to catch "this clearly isn't a LinkedIn
# profile URL" typos, not a strict validator.
_LINKEDIN_URL_RE = re.compile(r"linkedin\.com/(in|profile|sales)/", re.IGNORECASE)


class _SafeDict(dict):
    """Used with str.format_map so a missing placeholder doesn't crash --
    it's left in the text as-is (e.g. {missing_field}) so it's obvious to
    the user when they review the queue, instead of raising a KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def load_table(file_path: str | Path) -> pd.DataFrame:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, dtype=str)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")
    return _clean_table(df)


def load_table_from_upload(uploaded_file) -> pd.DataFrame:
    """uploaded_file: a Streamlit UploadedFile (file-like, has .name)."""
    name = getattr(uploaded_file, "name", "") or ""
    suffix = Path(name).suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(uploaded_file, dtype=str)
    elif suffix == ".csv":
        df = pd.read_csv(uploaded_file, dtype=str)
    else:
        raise ValueError(f"Unsupported file type: {suffix or '(unknown)'}")
    return _clean_table(df)


def _clean_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.fillna("")
    # Normalize column names to strings (pandas can infer non-str headers).
    df.columns = [str(c) for c in df.columns]
    return df


def render_template(template: str, row: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return string.Formatter().vformat(template, (), _SafeDict(**row))
    except (ValueError, IndexError):
        # Template has stray braces the user didn't mean as placeholders
        # (e.g. literal "{" in the message) -- fall back to the raw text
        # rather than breaking the import.
        return template


def looks_like_linkedin_url(value: str) -> bool:
    return bool(_LINKEDIN_URL_RE.search(value or ""))


def validate_mapping(df: pd.DataFrame, mapping: dict) -> list[str]:
    warnings: list[str] = []
    profile_col = mapping.get("profile_url")
    if not profile_col:
        warnings.append("No LinkedIn profile URL column selected.")
    elif profile_col in df.columns:
        sample = [v for v in df[profile_col].head(25).tolist() if v]
        bad = [v for v in sample if not looks_like_linkedin_url(v)]
        if sample and len(bad) == len(sample):
            warnings.append(
                f"None of the sampled values in '{profile_col}' look like LinkedIn "
                "profile URLs (expected something containing linkedin.com/in/...). "
                "Double-check the column mapping."
            )
        elif bad:
            warnings.append(
                f"{len(bad)} of the first {len(sample)} rows in '{profile_col}' don't "
                "look like LinkedIn profile URLs -- they'll still be imported, just "
                "worth a glance before you start sending."
            )

    message_cols = [mapping.get(f"message_{i}") for i in range(1, MAX_MESSAGES + 1)]
    if not message_cols[0]:
        warnings.append("No column selected for Message 1 -- at least the first message is required.")

    dupes = df[profile_col].duplicated().sum() if profile_col and profile_col in df.columns else 0
    if dupes:
        warnings.append(f"{dupes} duplicate profile URLs found in the sheet.")

    return warnings


def build_leads_from_df(df: pd.DataFrame, mapping: dict) -> list[dict]:
    """mapping keys: profile_url, name, company, message_1..message_5
    (name/company/message_2..5 are optional -- pass '' or omit)."""
    profile_col = mapping["profile_url"]
    name_col = mapping.get("name") or None
    company_col = mapping.get("company") or None
    message_cols = [mapping.get(f"message_{i}") or None for i in range(1, MAX_MESSAGES + 1)]

    leads: list[dict] = []
    for idx, row in df.iterrows():
        row_dict = row.to_dict()
        profile_url = str(row_dict.get(profile_col, "")).strip()
        if not profile_url:
            continue  # skip blank rows rather than importing a dead lead

        messages = []
        for col in message_cols:
            if not col:
                break
            raw = str(row_dict.get(col, "") or "")
            if not raw.strip():
                break  # stop at the first empty message slot for this row
            messages.append(render_template(raw, row_dict))

        leads.append(
            {
                "row_index": int(idx),
                "profile_url": profile_url,
                "display_name": str(row_dict.get(name_col, "")).strip() if name_col else "",
                "company": str(row_dict.get(company_col, "")).strip() if company_col else "",
                "raw_data": row_dict,
                "messages": messages,
            }
        )
    return leads
