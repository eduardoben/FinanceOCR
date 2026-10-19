"""
utils.py - Export Utilities and Clipboard Integration for Excel

Provides fast conversions to:
1. Multi-sheet Excel (.xlsx) workbooks via openpyxl
2. Tab-Separated Values (TSV) for direct Ctrl+V copy-paste into Excel
3. Comma-Separated Values (.csv) files
4. Interactive client-side clipboard JavaScript components
"""

import io
import json
from typing import List, Optional, Dict, Any
import pandas as pd
import streamlit as st

from models import Transaccion, TipoTransaccion, ReconciliacionResultado
from parsers import infer_statement_mode



AUDIT_COLUMNS = [
    "id", "pagina", "fecha", "concepto", "importe", "descripción",
    "tipo", "saldo_renglon", "confianza", "origen", "estado_revision", "linea_origen"
]
AUDIT_COLS_TO_DROP = [
    "id", "pagina", "confianza", "origen", "estado_revision",
    "linea_origen", "saldo_renglon", "tipo"
]


def create_standard_dataframe(transactions: list, include_audit: bool = False) -> pd.DataFrame:
    """
    Normalizes a list of Transaccion models into a clean pandas DataFrame.
    If include_audit is False (default), returns the 5 canonical columns:
    [fecha, concepto, importe, descripción, tipo].
    If include_audit is True, returns extended audit columns including
    [id, pagina, fecha, concepto, importe, descripción, tipo, saldo_renglon, confianza, origen, estado_revision, linea_origen].
    """
    if not transactions:
        cols = AUDIT_COLUMNS if include_audit else ["fecha", "concepto", "importe", "descripción", "tipo"]
        return pd.DataFrame(columns=cols)

    records = []
    for tx in transactions:
        tipo_str = tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
        rec = {
            "fecha": tx.fecha,
            "concepto": tx.concepto,
            "importe": round(float(tx.importe), 2),
            "descripción": tx.descripcion,
            "tipo": tipo_str
        }
        if include_audit:
            rec["id"] = getattr(tx, "id", None)
            rec["pagina"] = getattr(tx, "pagina", 1)
            rec["saldo_renglon"] = getattr(tx, "saldo_renglon", None)
            rec["confianza"] = getattr(tx, "confianza", 1.0)
            rec["origen"] = getattr(tx, "origen", "llm")
            rec["estado_revision"] = getattr(tx, "estado_revision", "pendiente")
            rec["linea_origen"] = getattr(tx, "linea_origen", "")
        records.append(rec)

    df = pd.DataFrame(records)
    # Order columns consistently
    if include_audit:
        ordered_cols = [c for c in AUDIT_COLUMNS if c in df.columns]
        df = df[ordered_cols]
    # Sort by fecha descending/ascending if valid dates
    try:
        df["_sort_date"] = pd.to_datetime(df["fecha"], errors="coerce")
        df = df.sort_values(by="_sort_date", ascending=True).drop(columns=["_sort_date"])
    except Exception:
        pass

    return df


def create_audit_dataframe(transactions: list) -> pd.DataFrame:
    """Convenience helper to build a full audit DataFrame with trace columns."""
    return create_standard_dataframe(transactions, include_audit=True)


def df_to_excel_bytes(df_ingresos: pd.DataFrame, df_egresos: pd.DataFrame) -> bytes:
    """
    Generates an in-memory .xlsx file with two distinct sheets:
    'Ingresos' and 'Egresos', properly formatted with openpyxl,
    excluding internal audit columns.
    """
    output = io.BytesIO()
    
    # Drop audit and 'tipo' columns before writing to individual sheets
    clean_ingresos = df_ingresos.drop(columns=AUDIT_COLS_TO_DROP, errors="ignore")
    clean_egresos = df_egresos.drop(columns=AUDIT_COLS_TO_DROP, errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        clean_ingresos.to_excel(writer, sheet_name="Ingresos", index=False)
        clean_egresos.to_excel(writer, sheet_name="Egresos", index=False)

    return output.getvalue()


def df_to_tsv(df: pd.DataFrame) -> str:
    """
    Converts DataFrame to a Tab-Separated Values string without row index,
    excluding internal audit columns.
    """
    clean_df = df.drop(columns=AUDIT_COLS_TO_DROP, errors="ignore")
    return clean_df.to_csv(sep="\t", index=False)


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Converts DataFrame to CSV bytes with UTF-8 BOM so Excel on Windows
    renders Spanish accents correctly, excluding internal audit columns.
    """
    clean_df = df.drop(columns=AUDIT_COLS_TO_DROP, errors="ignore")
    return clean_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def render_clipboard_button(tsv_data: str, label: str = "Copiar a Portapapeles (Formato Excel)", button_id: str = "copy_btn"):
    """
    Injects a client-side JavaScript button into Streamlit that copies TSV data
    directly to the operating system clipboard for immediate Ctrl+V into Excel.
    """
    # Safe JSON encoding of the TSV string to prevent injection or quote breaking
    json_tsv = json.dumps(tsv_data)
    
    html_code = f"""
    <div style="margin: 8px 0;">
        <button id="{button_id}" onclick="copyTSV_{button_id}()" style="
            background: linear-gradient(135deg, #107C41 0%, #0B552C 100%);
            color: #ffffff;
            border: 1px solid #0B552C;
            border-radius: 6px;
            padding: 8px 16px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            transition: all 0.2s ease;
        ">
            📋 {label}
        </button>
        <span id="status_{button_id}" style="
            margin-left: 10px;
            font-family: system-ui, sans-serif;
            font-size: 13px;
            color: #107C41;
            font-weight: 500;
        "></span>
        <script>
            function copyTSV_{button_id}() {{
                const text = {json_tsv};
                navigator.clipboard.writeText(text).then(function() {{
                    const status = document.getElementById("status_{button_id}");
                    const btn = document.getElementById("{button_id}");
                    status.innerText = "¡Copiado! Listo para pegar en Excel (Ctrl + V)";
                    btn.style.opacity = "0.8";
                    setTimeout(function() {{
                        status.innerText = "";
                        btn.style.opacity = "1";
                    }}, 4000);
                }}).catch(function(err) {{
                    const status = document.getElementById("status_{button_id}");
                    status.style.color = "#d32f2f";
                    status.innerText = "Error al copiar. Permite acceso al portapapeles en el navegador.";
                }});
            }}
        </script>
    </div>
    """
    try:
        st.html(html_code, unsafe_allow_javascript=True)
    except Exception:
        st.components.v1.html(html_code, height=50)


def reconcile_transactions(
    transactions: List[Transaccion],
    saldo_inicial: Optional[float],
    saldo_final: Optional[float],
    modo_contable: Optional[str] = None,
    text: Optional[str] = None,
    summary_data: Optional[Dict[str, Any]] = None,
) -> ReconciliacionResultado:
    """
    Performs arithmetic balance reconciliation to verify 100% extraction certainty.
    Eliminates dual-formula guesswork: strictly uses inferred or explicit accounting mode.
    If mode is 'ambiguo', reconciliation is marked as cuadra = False.
    Calculates running balance chain compliance if rows contain saldo_renglon.

    Args:
        transactions: List of extracted Transaccion items.
        saldo_inicial: Stated opening balance from document or None.
        saldo_final: Stated closing balance from document or None.
        modo_contable: Optional explicit mode ('debito', 'credito', 'ambiguo').
        text: Optional raw statement text for automatic mode inference.
        summary_data: Optional dictionary with declared summary figures.

    Returns:
        ReconciliacionResultado with calculations, difference, certainty status, and chain metrics.
    """
    total_ingresos = round(
        sum(float(tx.importe) for tx in transactions if tx.tipo == TipoTransaccion.INGRESO or str(tx.tipo).lower() in ("ingreso", "abono")),
        2
    )
    total_egresos = round(
        sum(float(tx.importe) for tx in transactions if tx.tipo == TipoTransaccion.EGRESO or str(tx.tipo).lower() in ("egreso", "cargo")),
        2
    )

    if saldo_inicial is None or saldo_final is None:
        return ReconciliacionResultado(
            saldo_inicial=saldo_inicial,
            saldo_final_declarado=saldo_final,
            saldo_final_esperado=saldo_final,
            saldo_final_calculado=None,
            total_ingresos=total_ingresos,
            total_egresos=total_egresos,
            total_abonos=total_ingresos,
            total_cargos=total_egresos,
            diferencia=None,
            cuadra=None,
            conciliado=None,
            modo_calculo="no_disponible",
            modo_contable="no_disponible"
        )

    s_ini = round(float(saldo_inicial), 2)
    s_fin = round(float(saldo_final), 2)

    sum_data = dict(summary_data) if summary_data else {}
    if "saldo_inicial" not in sum_data and s_ini is not None:
        sum_data["saldo_inicial"] = s_ini
    if "saldo_final" not in sum_data and s_fin is not None:
        sum_data["saldo_final"] = s_fin
    if "total_cargos" not in sum_data:
        sum_data["total_cargos"] = total_egresos
    if "total_abonos" not in sum_data:
        sum_data["total_abonos"] = total_ingresos

    # Resolve modo_contable deterministically without guessing or picking min(diff)
    effective_mode = modo_contable
    if not effective_mode:
        comb_text = text or ""
        if not comb_text and transactions:
            comb_text = " ".join(f"{tx.concepto} {tx.descripcion}" for tx in transactions)
        effective_mode = infer_statement_mode(comb_text, sum_data)

    # Evaluate running balance chain if saldo_renglon is available on transaction rows
    filas_con_saldo = 0
    filas_cumplen = 0
    running_bal = s_ini
    for tx in transactions:
        row_bal = getattr(tx, "saldo_renglon", None)
        if row_bal is not None:
            filas_con_saldo += 1
            is_ingreso = tx.tipo == TipoTransaccion.INGRESO or str(tx.tipo).lower() in ("ingreso", "abono")
            if effective_mode == "debito":
                expected_row = running_bal + tx.importe if is_ingreso else running_bal - tx.importe
            elif effective_mode == "credito":
                expected_row = running_bal - tx.importe if is_ingreso else running_bal + tx.importe
            else:
                expected_row = running_bal
            if abs(round(expected_row - row_bal, 2)) <= 0.01:
                filas_cumplen += 1
            running_bal = row_bal

    cumplimiento_cadena = round((filas_cumplen / filas_con_saldo) * 100, 2) if filas_con_saldo > 0 else None

    # Apply strict formula strictly based on the resolved mode
    if effective_mode == "debito":
        calc_final = round(s_ini + total_ingresos - total_egresos, 2)
        diff = round(abs(calc_final - s_fin), 2)
        cuadra = diff <= 0.01
    elif effective_mode == "credito":
        calc_final = round(s_ini - total_ingresos + total_egresos, 2)
        diff = round(abs(calc_final - s_fin), 2)
        cuadra = diff <= 0.01
    else:  # "ambiguo"
        effective_mode = "ambiguo"
        calc_final = round(s_ini + total_ingresos - total_egresos, 2)
        diff = round(abs(calc_final - s_fin), 2)
        cuadra = False  # Ambiguous mode NEVER reconciles to True

    conciliado = cuadra

    return ReconciliacionResultado(
        saldo_inicial=s_ini,
        saldo_final_declarado=s_fin,
        saldo_final_esperado=s_fin,
        saldo_final_calculado=calc_final,
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        total_abonos=total_ingresos,
        total_cargos=total_egresos,
        diferencia=diff,
        cuadra=cuadra,
        conciliado=conciliado,
        modo_calculo=effective_mode,
        modo_contable=effective_mode,
        cumplimiento_cadena_saldo=cumplimiento_cadena,
        filas_con_saldo=filas_con_saldo,
        filas_cumplen_cadena=filas_cumplen
    )

