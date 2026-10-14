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
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # 1b. Check for compact YYYYMMDD or YYYYMMDDHHMMSS (e.g. OFX / banking exports)
    compact_match = re.search(r"\b(\d{4})(\d{2})(\d{2})(?:\d{2,6})?\b", cleaned)
    if compact_match:
        y, m, d = int(compact_match.group(1)), int(compact_match.group(2)), int(compact_match.group(3))
        if 1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

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

            if 1 <= day <= 31 and 1 <= month <= 12:
                return f"{year:04d}-{month:02d}-{day:02d}"

    # 3. Check for DD/MM/YYYY or DD-MM-YYYY or DD.MM.YYYY
    dmy_match = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", cleaned)
    if dmy_match:
        d, m, y_str = int(dmy_match.group(1)), int(dmy_match.group(2)), dmy_match.group(3)
        y = int(y_str)
        if y < 100:
            y += 2000

        # Handle ambiguous case where d and m might be reversed or valid
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"
        elif 1 <= d <= 12 and 1 <= m <= 31:
            # Swapped MM/DD/YYYY
            return f"{y:04d}-{d:02d}-{m:02d}"

    # 4. Check for DD/MM without year (uses default_year or current year)
    dm_match = re.search(r"(\d{1,2})[-/](\d{1,2})", cleaned)
    if dm_match:
        d, m = int(dm_match.group(1)), int(dm_match.group(2))
        y = default_year or datetime.now().year
        if 1 <= m <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{m:02d}-{d:02d}"

    # Fallback: Return as is if already 10 chars or fallback to today
    return cleaned[:10] if len(cleaned) >= 10 else datetime.now().strftime("%Y-%m-%d")
