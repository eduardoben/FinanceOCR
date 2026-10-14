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
import pandas as pd
import streamlit as st


def create_standard_dataframe(transactions: list) -> pd.DataFrame:
    """
    Normalizes a list of Transaccion models into a clean pandas DataFrame
    with canonical column names: fecha, concepto, importe, descripción, tipo.
    """
    if not transactions:
        return pd.DataFrame(columns=["fecha", "concepto", "importe", "descripción", "tipo"])

    records = []
    for tx in transactions:
        records.append({
            "fecha": tx.fecha,
            "concepto": tx.concepto,
            "importe": round(float(tx.importe), 2),
            "descripción": tx.descripcion,
            "tipo": tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
        })

    df = pd.DataFrame(records)
    # Sort by fecha descending/ascending if valid dates
    try:
        df["_sort_date"] = pd.to_datetime(df["fecha"], errors="coerce")
        df = df.sort_values(by="_sort_date", ascending=True).drop(columns=["_sort_date"])
    except Exception:
        pass

    return df


def df_to_excel_bytes(df_ingresos: pd.DataFrame, df_egresos: pd.DataFrame) -> bytes:
    """
    Generates an in-memory .xlsx file with two distinct sheets:
    'Ingresos' and 'Egresos', properly formatted with openpyxl.
    """
    output = io.BytesIO()
    
    # Drop 'tipo' column if present before writing to individual sheets
    clean_ingresos = df_ingresos.drop(columns=["tipo"], errors="ignore")
    clean_egresos = df_egresos.drop(columns=["tipo"], errors="ignore")

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        clean_ingresos.to_excel(writer, sheet_name="Ingresos", index=False)
        clean_egresos.to_excel(writer, sheet_name="Egresos", index=False)

    return output.getvalue()


def df_to_tsv(df: pd.DataFrame) -> str:
    """
    Converts DataFrame to a Tab-Separated Values string without row index.
    This is the exact format Excel reads when pasting with Ctrl+V.
    """
    clean_df = df.drop(columns=["tipo"], errors="ignore")
    return clean_df.to_csv(sep="\t", index=False)


def df_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Converts DataFrame to CSV bytes with UTF-8 BOM so Excel on Windows
    renders Spanish accents (tildes, ñ) correctly.
    """
    clean_df = df.drop(columns=["tipo"], errors="ignore")
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
    st.components.v1.html(html_code, height=50)
