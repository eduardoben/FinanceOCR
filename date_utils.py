"""
date_utils.py - Robust Spanish Banking Date Normalizer

Normalizes varied Mexican and Spanish bank statement date representations
into the canonical ISO 8601 'YYYY-MM-DD' format required for Excel copy/paste.
"""

import re
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

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


def resolve_year_for_month(
    month: int,
    default_year: Optional[int] = None,
    statement_period: Optional[Any] = None
) -> int:
    """
    Infers the correct calendar year for a given month extracted from a bank statement,
    properly resolving statement periods spanning across years (e.g. December -> January).
    """
    current_year = default_year or datetime.now().year

    if not statement_period:
        return current_year

    # 1. Statement period as a tuple/list: (start_date, end_date)
    if isinstance(statement_period, (tuple, list)) and len(statement_period) >= 2:
        s, e = statement_period[0], statement_period[1]
        s_dt = s if isinstance(s, datetime) else None
        e_dt = e if isinstance(e, datetime) else None
        if not s_dt and isinstance(s, str):
            try:
                s_dt = datetime.fromisoformat(s.strip()[:10])
            except Exception:
                pass
        if not e_dt and isinstance(e, str):
            try:
                e_dt = datetime.fromisoformat(e.strip()[:10])
            except Exception:
                pass

        if s_dt and e_dt:
            if month == s_dt.month:
                return s_dt.year
            if month == e_dt.month:
                return e_dt.year
            if s_dt.year != e_dt.year:
                # Cross-year: e.g. Dec (month 12) -> s_dt.year, Jan (month 1) -> e_dt.year
                if month >= s_dt.month:
                    return s_dt.year
                elif month <= e_dt.month:
                    return e_dt.year
            return e_dt.year

    # 2. Statement period as dictionary: e.g. {"start_month": 12, "start_year": 2023, "end_month": 1, "end_year": 2024}
    if isinstance(statement_period, dict):
        s_m = statement_period.get("start_month")
        s_y = statement_period.get("start_year")
        e_m = statement_period.get("end_month")
        e_y = statement_period.get("end_year")
        if s_m and s_y and month == s_m:
            return s_y
        if e_m and e_y and month == e_m:
            return e_y
        if s_y and e_y and s_y != e_y:
            if s_m and month >= s_m:
                return s_y
            if e_m and month <= e_m:
                return e_y
        if e_y:
            return e_y
        if s_y:
            return s_y

    # 3. Statement period as text string: e.g. "16 Dic 25 - 15 Ene 26", "16/12/2023 - 15/01/2024", "Diciembre 2023 a Enero 2024"
    if isinstance(statement_period, str):
        parsed = extract_statement_period(statement_period)
        if parsed:
            return resolve_year_for_month(month, default_year=default_year, statement_period=parsed)

    return current_year


def extract_statement_period(text: str) -> Optional[Dict[str, int]]:
    """
    Parses start and end dates from a statement period string to extract months and years.
    Handles:
    - '16 Dic 25 - 15 Ene 26'
    - '16/12/2023 - 15/01/2024'
    - '01/01/2026-31/01/2026'
    - 'del 16 de diciembre de 2023 al 15 de enero de 2024'
    """
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip().lower()

    # Split on period range delimiters: "-", " al ", " a ", " to "
    parts = re.split(r"\s*(?:-|–|—|\bal\b|\ba\b)\s*", cleaned)
    if len(parts) >= 2:
        part_a, part_b = parts[0], parts[1]

        def _extract_m_y(s: str) -> Tuple[Optional[int], Optional[int]]:
            # Textual month e.g. "16 dic 25", "diciembre 2023", "16 de diciembre de 2023"
            for m_name, m_num in SPANISH_MONTH_MAP.items():
                m_match = re.search(rf"\b{m_name}\b", s)
                if m_match:
                    # Look for year following the month name first
                    after_month = s[m_match.end():]
                    y_after = re.search(r"\b(20[1-3]\d|\d{2})\b", after_month)
                    if y_after:
                        y_val = int(y_after.group(1))
                        if y_val < 100:
                            y_val += 2000
                        return m_num, y_val
                    # Otherwise look for explicit 4-digit year anywhere in s
                    y_4d = re.search(r"\b(20[1-3]\d)\b", s)
                    if y_4d:
                        return m_num, int(y_4d.group(1))
                    return m_num, None

            # Numeric month e.g. "16/12/2023" or "16-12-25"
            dmy = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](20[1-3]\d|\d{2})", s)
            if dmy:
                m_val = int(dmy.group(2))
                y_val = int(dmy.group(3))
                if y_val < 100:
                    y_val += 2000
                return m_val, y_val
            return None, None

        m_a, y_a = _extract_m_y(part_a)
        m_b, y_b = _extract_m_y(part_b)

        # If only end has explicit year, infer start year
        if y_b and not y_a:
            if m_a and m_b and m_a > m_b:
                # Dec -> Jan transition
                y_a = y_b - 1
            else:
                y_a = y_b

        if m_a and y_a and m_b and y_b:
            return {
                "start_month": m_a,
                "start_year": y_a,
                "end_month": m_b,
                "end_year": y_b,
            }

    return None


def normalize_spanish_date(
    date_str: str,
    default_year: Optional[int] = None,
    statement_period: Optional[Any] = None
) -> str:
    """
    Takes any arbitrary date string extracted from a Spanish bank statement
    and converts it strictly to canonical ISO YYYY-MM-DD.

    Handles banking dates without explicit year (e.g. '15/ENE', '03-FEB', '12 MAR', '15/01')
    inferring the year from the statement period (with December-January cross-year transitions).

    Examples handled:
    - '15/01/2024' -> '2024-01-15'
    - '15-01-2024' -> '2024-01-15'
    - '15/01/24'   -> '2024-01-15'
    - '15 ENE 2024' -> '2024-01-15'
    - '15-ENE-24'   -> '2024-01-15'
    - '15/ENE'      -> 'YYYY-01-15' (inferred from period/year)
    - '03-FEB'      -> 'YYYY-02-03'
    - '12 MAR'      -> 'YYYY-03-12'
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

    # 2. Check for textual month format:
    # Day-first: e.g. "15 ENE 2024", "15-ENE-24", "15/ENE", "03-FEB", "12 MAR", "15 de enero de 2024"
    text_month_match = re.search(
        r"(\d{1,2})\s*(?:del|de|\/|-|\s)\s*([a-z]{3,10})\.?\s*(?:del|de|\/|-|\s)*(\d{2,4})?",
        cleaned
    )
    if text_month_match:
        day_str, month_str, year_str = text_month_match.groups()
        month_clean = month_str.rstrip(".")
        if month_clean in SPANISH_MONTH_MAP:
            day = int(day_str)
            month = SPANISH_MONTH_MAP[month_clean]
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = resolve_year_for_month(month, default_year=default_year, statement_period=statement_period)

            valid_iso = _build_valid_iso_date(year, month, day)
            if valid_iso:
                return valid_iso

    # Month-first textual format: e.g. "ENE 15", "FEB-03 2024", "MAR 12"
    month_first_match = re.search(
        r"\b([a-z]{3,10})\.?\s*(?:del|de|\/|-|\s)\s*(\d{1,2})\s*(?:del|de|\/|-|\s)*(\d{2,4})?",
        cleaned
    )
    if month_first_match:
        month_str, day_str, year_str = month_first_match.groups()
        month_clean = month_str.rstrip(".")
        if month_clean in SPANISH_MONTH_MAP:
            day = int(day_str)
            month = SPANISH_MONTH_MAP[month_clean]
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = resolve_year_for_month(month, default_year=default_year, statement_period=statement_period)

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

    # 4. Check for DD/MM without year (uses statement_period / default_year / current year)
    dm_match = re.search(r"(\d{1,2})[-/](\d{1,2})", cleaned)
    if dm_match:
        d, m = int(dm_match.group(1)), int(dm_match.group(2))
        y = resolve_year_for_month(m, default_year=default_year, statement_period=statement_period)
        valid_iso = _build_valid_iso_date(y, m, d)
        if valid_iso:
            return valid_iso

    # Fallback: Return as is if already 10 chars (YYYY-MM-DD) or fallback to today
    if len(cleaned) == 10 and re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
        return cleaned
    return datetime.now().strftime("%Y-%m-%d")


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

