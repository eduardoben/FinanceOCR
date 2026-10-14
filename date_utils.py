"""
date_utils.py - Robust Spanish Banking Date Normalizer

Normalizes varied Mexican and Spanish bank statement date representations
into the canonical ISO 8601 'YYYY-MM-DD' format required for Excel copy/paste.
"""

import re
from datetime import datetime
from typing import Optional

SPANISH_MONTH_MAP = {
    "ene": 1, "enero": 1, "jan": 1,
    "feb": 2, "febrero": 2,
    "mar": 3, "marzo": 3,
    "abr": 4, "abril": 4, "apr": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6,
    "jul": 7, "julio": 7,
    "ago": 8, "agosto": 8, "aug": 8,
    "sep": 9, "sept": 9, "set": 9, "septiembre": 9, "setiembre": 9,
    "oct": 10, "octubre": 10,
    "nov": 11, "noviembre": 11,
    "dic": 12, "diciembre": 12, "dec": 12,
}


def _build_valid_iso_date(year: int, month: int, day: int) -> Optional[str]:
    """
    Validates calendar date existence (rejects invalid dates like 2024-02-31).
    Returns canonical ISO 'YYYY-MM-DD' on success, or None on ValueError.
    """
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


def normalize_spanish_date(date_str: str, default_year: Optional[int] = None) -> str:
    """
    Takes any arbitrary date string extracted from a Spanish bank statement
    and converts it to strictly YYYY-MM-DD.

    Examples handled:
    - '15/01/2024' -> '2024-01-15'
    - '15-01-2024' -> '2024-01-15'
    - '15/01/24'   -> '2024-01-15'
    - '15 ENE 2024' -> '2024-01-15'
    - '15-ENE-24'   -> '2024-01-15'
    - '15 DE ENERO DE 2024' -> '2024-01-15'
    - '2024-01-15T12:00:00' -> '2024-01-15' (CFDI ISO format)
    - '2024-01-15' -> '2024-01-15'
    """
    if not date_str or not isinstance(date_str, str):
        return datetime.now().strftime("%Y-%m-%d")

    cleaned = date_str.strip().lower()

    # 1. Direct check for ISO format YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
    iso_match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", cleaned)
    if iso_match:
        y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
        valid_iso = _build_valid_iso_date(y, m, d)
        if valid_iso:
            return valid_iso

    # 1b. Check for compact YYYYMMDD or YYYYMMDDHHMMSS (e.g. OFX / banking exports)
    compact_match = re.search(r"\b(\d{4})(\d{2})(\d{2})(?:\d{2,6})?\b", cleaned)
    if compact_match:
        y, m, d = int(compact_match.group(1)), int(compact_match.group(2)), int(compact_match.group(3))
        if 1900 <= y <= 2100:
            valid_iso = _build_valid_iso_date(y, m, d)
            if valid_iso:
                return valid_iso

    # 2. Check for textual month format: e.g. "15 ENE 2024", "15-ENE-24", "15 de enero de 2024", "28 de febrero del 2024"
    text_month_match = re.search(
        r"(\d{1,2})\s*(?:del|de|\/|-|\s)\s*([a-z]{3,10})\s*(?:del|de|\/|-|\s)*(\d{2,4})?",
        cleaned
    )
    if text_month_match:
        day_str, month_str, year_str = text_month_match.groups()
        if month_str in SPANISH_MONTH_MAP:
            day = int(day_str)
            month = SPANISH_MONTH_MAP[month_str]
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = default_year or datetime.now().year

            valid_iso = _build_valid_iso_date(year, month, day)
            if valid_iso:
                return valid_iso

    # 3. Check for DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    dmy_match = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", cleaned)
    if dmy_match:
        d, m, y_str = int(dmy_match.group(1)), int(dmy_match.group(2)), dmy_match.group(3)
        y = int(y_str)
        if y < 100:
            y += 2000

        # Try DD/MM/YYYY first
        valid_iso = _build_valid_iso_date(y, m, d)
        if valid_iso:
            return valid_iso
        # Fallback to MM/DD/YYYY if ambiguous
        valid_iso_swapped = _build_valid_iso_date(y, d, m)
        if valid_iso_swapped:
            return valid_iso_swapped

    # 4. Check for DD/MM without year (uses default_year or current year)
    dm_match = re.search(r"(\d{1,2})[-/](\d{1,2})", cleaned)
    if dm_match:
        d, m = int(dm_match.group(1)), int(dm_match.group(2))
        y = default_year or datetime.now().year
        valid_iso = _build_valid_iso_date(y, m, d)
        if valid_iso:
            return valid_iso

    # Fallback: Return as is if already 10 chars or fallback to today
    return cleaned[:10] if len(cleaned) >= 10 else datetime.now().strftime("%Y-%m-%d")


def extract_statement_year(text: str) -> Optional[int]:
    """
    Searches document text or headers for the statement period year (2015-2035)
    to serve as default_year when table rows only contain DD/MONTH.
    """
    if not text or not isinstance(text, str):
        return None

    # Search for period statements e.g. "periodo del 01 al 31 de enero de 2024", "al 31/12/2023"
    patterns = [
        r"(?:periodo|ejercicio|corte|al|del|fecha)[^\d\n]{0,40}\b(20[1-3]\d)\b",
        r"\b(\d{1,2})[-/.](?:[a-z]{3,10}|\d{1,2})[-/.](20[1-3]\d)\b",
        r"\b(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)[^\d\n]{0,25}\b(20[1-3]\d)\b",
        r"\b(20[1-3]\d)\b"
    ]
    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for m in matches:
            groups = m.groups()
            y_val = groups[-1] if groups else m.group(0)
            try:
                y_int = int(y_val)
                if 2015 <= y_int <= 2035:
                    return y_int
            except ValueError:
                continue
    return None

