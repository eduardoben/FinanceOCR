"""
app.py - Local Financial Bank Statement Extractor & Transaction History

100% local, privacy-first web application to extract financial transactions
from bank statements (PDF and XML), classify into Ingresos & Egresos,
persist in local SQLite database with duplicate detection, and
export or copy directly into Excel with standard formatting.
"""

import os
import sys
import importlib
import tempfile
import multiprocessing
from datetime import datetime
import pandas as pd
import streamlit as st

# Hot-reload local modules if already in sys.modules so running Streamlit servers pick up code updates immediately
for _mod_name in ["models", "date_utils", "database", "parsers", "llm_client", "telemetry", "utils"]:
    if _mod_name in sys.modules:
        try:
            importlib.reload(sys.modules[_mod_name])
        except Exception:
            pass

from models import Transaccion, TipoTransaccion
from parsers import (
    parse_pdf,
    parse_xml,
    detect_pdf_profile,
    render_pdf_page_image,
    resolve_pdf_strategy,
    resolve_xml_strategy,
)
from llm_client import check_ollama_connection, extract_transactions_with_ollama
from telemetry import render_telemetry_expander, StageStopwatch
from utils import (
    create_standard_dataframe,
    df_to_excel_bytes,
    df_to_tsv,
    df_to_csv_bytes,
    render_clipboard_button,
)
from database import (
    compute_file_hash,
    check_file_processed,
    save_processed_transactions,
    query_transactions,
    get_distinct_files,
    delete_processed_file,
    get_database_stats,
    save_document_file,
    get_document_file_path,
    get_document_file_bytes,
    save_batch_checkpoint,
    get_batch_checkpoint,
    mark_checkpoint_file_completed,
    mark_checkpoint_interrupted,
    clear_batch_checkpoint,
)

# Page configuration
st.set_page_config(
    page_title="Extractor Financiero Local | PDF & XML",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling for Financial Dashboard
st.markdown("""
<style>
    .metric-card {
        background: #ffffff;
        border: 1px solid #e0e0e0;
        border-radius: 10px;
        padding: 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }
    .metric-value-ingreso {
        font-size: 24px;
        font-weight: 700;
        color: #2e7d32;
    }
    .metric-value-egreso {
        font-size: 24px;
        font-weight: 700;
        color: #c62828;
    }
    .metric-value-neto {
        font-size: 24px;
        font-weight: 700;
        color: #1565c0;
    }
    .badge-local {
        background-color: #e8f5e9;
        color: #2e7d32;
        padding: 4px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
    }
    .badge-digital {
        background-color: #e3f2fd;
        color: #1565c0;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
    }
    .badge-ocr {
        background-color: #fff3e0;
        color: #e65100;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
    }
    .badge-nuevo {
        background-color: #e8f5e9;
        color: #2e7d32;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
    }
    .badge-duplicado {
        background-color: #fff8e1;
        color: #f57f17;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 12px;
    }
</style>
""", unsafe_allow_html=True)


def render_statement_inspector(file_record: dict, key_prefix: str = "insp") -> None:
    """
    Renders an interactive bank statement inspector allowing the user to view,
    examine pages, inspect raw XML, and download the original file.
    """
    fname = file_record.get("nombre_archivo", "Desconocido")
    fpath = file_record.get("ruta_archivo")
    ftype = file_record.get("tipo_archivo", "pdf").lower()
    fid = file_record.get("id", 0)

    if not fpath or not os.path.exists(fpath):
        st.warning(
            f"⚠️ El archivo físico `{fname}` no está disponible en `data/documentos/`. "
            f"Si fue procesado antes de activar el almacenamiento persistente, vuelve a cargarlo para habilitar el visor."
        )
        return

    try:
        with open(fpath, "rb") as f_in:
            file_bytes = f_in.read()
    except Exception as read_err:
        st.error(f"❌ Error al leer el archivo desde el almacenamiento persistente: {str(read_err)}")
        return

    size_kb = len(file_bytes) / 1024
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{(size_kb / 1024):.2f} MB"

    c_meta, c_dl = st.columns([3, 1])
    with c_meta:
        st.markdown(
            f"📄 **{fname}** &nbsp;|&nbsp; 🏷️ **Tipo:** `{ftype.upper()}` &nbsp;|&nbsp; "
            f"📦 **Tamaño:** `{size_str}` &nbsp;|&nbsp; 📅 **Procesado:** `{file_record.get('fecha_procesamiento', 'N/D')}`"
        )
    with c_dl:
        mime_type = "application/pdf" if ftype == "pdf" else "application/xml"
        st.download_button(
            "📥 Descargar Original",
            data=file_bytes,
            file_name=fname,
            mime=mime_type,
            key=f"{key_prefix}_dl_{fid}",
            width="stretch"
        )

    st.write("")

    if ftype == "pdf":
        try:
            profile = detect_pdf_profile(file_bytes)
            page_count = profile["page_count"]

            c_ctrl, c_view = st.columns([1, 2])
            with c_ctrl:
                sel_p = st.number_input(
                    "Página a visualizar:",
                    min_value=1,
                    max_value=page_count,
                    value=1,
                    key=f"{key_prefix}_pnum_{fid}"
                )
                st.caption(f"Mostrando página {sel_p} de {page_count}.")
                zoom = st.select_slider(
                    "Resolución de Vista:",
                    options=[0.75, 1.0, 1.25, 1.5],
                    value=1.0,
                    key=f"{key_prefix}_zoom_{fid}"
                )
                show_thumbs = st.checkbox(
                    "🖼️ Ver miniaturas de todas las páginas",
                    value=False,
                    key=f"{key_prefix}_thumbs_{fid}"
                )

            with c_view:
                img = render_pdf_page_image(file_bytes, page_number=int(sel_p), scale=float(zoom))
                st.image(img, caption=f"Página {sel_p} de {page_count} — {fname}", width="stretch")

            if show_thumbs:
                st.markdown("##### 🖼️ Cuadrícula de Páginas")
                t_cols = st.columns(min(page_count, 4))
                for p_i in range(1, page_count + 1):
                    target_col = t_cols[(p_i - 1) % len(t_cols)]
                    try:
                        t_img = render_pdf_page_image(file_bytes, page_number=p_i, scale=0.35)
                        target_col.image(t_img, caption=f"Pág. {p_i}", width="stretch")
                    except Exception:
                        pass

        except Exception as pdf_err:
            st.error(f"Error al visualizar PDF: {str(pdf_err)}")

    elif ftype == "xml":
        try:
            xml_text = file_bytes.decode("utf-8", errors="replace")
            st.markdown("##### 📑 Contenido XML Estructurado")
            st.code(xml_text, language="xml", line_numbers=True)
        except Exception as xml_err:
            st.error(f"Error al decodificar XML: {str(xml_err)}")


def main():
    # ---------------------------------------------------------
    # 1. SIDEBAR CONFIGURATION
    # ---------------------------------------------------------
    with st.sidebar:
        st.title("🏦 Configuración Local")
        st.markdown("<span class='badge-local'>🔒 100% Local / Sin Telemetría</span>", unsafe_allow_html=True)
        st.write("")

        st.subheader("🤖 Servidor Ollama")
        ollama_url = st.text_input(
            "URL Base de Ollama",
            value="http://localhost:11434",
            help="Dirección del servidor local de Ollama"
        )
        model_name = st.text_input(
            "Nombre del Modelo",
            value="gemma4:12b",
            help="Modelo local en Ollama optimizado para extracción estructurada (e.g., gemma4:12b, llama3.1, qwen2.5)"
        )

        num_ctx = st.select_slider(
            "Ventana de Contexto Ollama (Tokens)",
            options=[2048, 4096, 8192, 16384, 32768],
            value=16384,
            help="16,384 tokens permite a Ollama procesar tablas completas de múltiples páginas sin truncar la respuesta JSON."
        )

        col_check, col_list = st.columns([1, 1])
        check_conn = st.button("🔌 Probar Conexión", width="stretch")

        # Connection status feedback
        if check_conn or "ollama_status" not in st.session_state:
            is_ok, msg, models = check_ollama_connection(ollama_url)
            st.session_state["ollama_status"] = is_ok
            st.session_state["ollama_msg"] = msg
            st.session_state["available_models"] = models

        if st.session_state.get("ollama_status"):
            st.success(f"✅ {st.session_state.get('ollama_msg')}")
            available = st.session_state.get("available_models", [])
            if model_name not in available and available:
                st.warning(f"⚠️ El modelo '{model_name}' no aparece en la lista local de Ollama. Modelos detectados: {', '.join(available)}")
        else:
            st.error(f"❌ {st.session_state.get('ollama_msg')}")

        st.divider()

        st.divider()

        st.subheader("🎯 Estrategias Generales de Extracción")
        st.caption("Configura las estrategias predeterminadas para cada formato. Todos los archivos heredan esta selección por defecto salvo anulación individual.")

        # 1. Estrategia General para PDF (Por defecto: Máxima Precisión)
        pdf_general_strategy = st.selectbox(
            "Estrategia General para PDFs",
            options=[
                "🎯 Máxima Precisión (Forzar RapidOCR + Accurate Tables + Escala 2.5x)",
                "🤖 Automático (Detecta Digital vs Escaneado + Fast Tables)",
                "⚡ Digital Nativo (10x Rápido - Omite OCR)",
                "🔍 Escaneo Físico (Forzar RapidOCR + Fast Tables)",
                "🛠️ Personalizado (Ajustar OCR, escala y tablas)"
            ],
            index=0,
            help="Define la estrategia predeterminada para todos los archivos PDF. 'Máxima Precisión' garantiza extracción exhaustiva de celdas complejas y sellos bancarios."
        )

        # 2. Estrategia General para XML (Por defecto: Máxima Precisión)
        xml_general_strategy = st.selectbox(
            "Estrategia General para XMLs",
            options=[
                "🎯 Máxima Precisión (Determinista Inteligente + Fallback Híbrido LLM)",
                "⚡ Determinista Estricto (CFDI, Addenda, OFX, CAMT - 0s latencia)",
                "🤖 Forzar Ollama LLM (Inferencia Generativa)"
            ],
            index=0,
            help="Define la estrategia predeterminada para todos los archivos XML. 'Máxima Precisión' analiza la estructura XML con reglas exactas y activa Ollama ante formatos ambiguos."
        )

        # Fine tuning parameters (if Personalizado is selected in General PDF)
        if pdf_general_strategy.startswith("🛠️ Personalizado"):
            ocr_mode_choice = st.selectbox(
                "Modo OCR en PDFs",
                options=[
                    "Automático (Detecta Digital vs Escaneado)",
                    "Forzar OCR (Siempre procesar imágenes)",
                    "Desactivar OCR (Solo texto nativo)",
                ],
                index=0,
                help="El modo Automático inspecciona el documento en <10ms: si detecta texto digital nativo omite OCR para una velocidad 10x superior. Si es escaneo físico, activa RapidOCR."
            )
            enable_ocr_val = None if ocr_mode_choice.startswith("Automático") else (True if ocr_mode_choice.startswith("Forzar") else False)

            table_mode_choice = st.selectbox(
                "Estructura de Tablas (TableFormer)",
                options=[
                    "Alta Velocidad (Fast - Recomendado)",
                    "Máxima Precisión (Accurate)",
                ],
                index=1,
                help="Fast procesa tablas regulares de banca en línea rápidamente. Accurate ejecuta múltiples pasadas para celdas complejas."
            )
            accurate_mode = (table_mode_choice == "Máxima Precisión (Accurate)")

            ocr_scale = st.select_slider(
                "Escala de Resolución OCR",
                options=[1.5, 2.0, 2.5, 3.0],
                value=2.5,
                help="2.5x proporciona nitidez óptima para tipografía bancaria pequeña."
            )
        else:
            resolved_gen_pdf = resolve_pdf_strategy(pdf_general_strategy, pdf_general_strategy)
            enable_ocr_val = resolved_gen_pdf["enable_ocr"]
            accurate_mode = resolved_gen_pdf["accurate_tables"]
            ocr_scale = resolved_gen_pdf["ocr_scale"]

        cpu_cores = multiprocessing.cpu_count() or 4
        default_threads = min(cpu_cores, 12)
        num_threads = st.slider(
            "Hilos de CPU para Inferencia Local",
            min_value=1,
            max_value=cpu_cores,
            value=default_threads,
            help=f"Tu equipo cuenta con {cpu_cores} núcleos de CPU. Asignar entre 4 y 12 hilos maximiza la velocidad de OCR y TableFormer."
        )

        user_rfc = st.text_input(
            "RFC de tu Empresa / Titular (Opcional)",
            value="",
            help="Para XML CFDI: Si el RFC coincide con el Emisor se clasifica como Ingreso, si coincide con Receptor como Egreso."
        )

        st.subheader("🔁 Resiliencia y Recuperación")
        error_handling_mode = st.radio(
            "Comportamiento ante error en lote",
            options=[
                "🛡️ Auto-Pausa y Reanudación (Guarda punto de control para volver donde se quedó)",
                "⏩ Continuar con restantes (Registra error y procesa los demás)"
            ],
            index=0,
            help="Si se produce un error, 'Auto-Pausa y Reanudación' resguarda todo el progreso previo en la base de datos y te permite reanudar automáticamente desde el archivo fallido sin re-procesar los anteriores."
        )
        auto_pause_on_error = error_handling_mode.startswith("🛡️ Auto-Pausa")

        st.divider()
        st.caption("Almacenamiento persistente local activo en data/finanzas.db.")

    # ---------------------------------------------------------
    # 2. MAIN HEADER
    # ---------------------------------------------------------
    st.header("📄 Extractor Inteligente de Estados de Cuenta")
    st.markdown(
        "Extrae transacciones bancarias desde archivos **PDF** (digitales y escaneados) y facturas/estados **XML**, "
        "clasifica automáticamente en **Ingresos** y **Egresos**, y gestiona tu **historial local persistente**."
    )

    # ---------------------------------------------------------
    # 3. TOP-LEVEL PERSISTENT NAVIGATION TABS
    # ---------------------------------------------------------
    tab_extractor, tab_historial = st.tabs([
        "📥 Extractor y Procesamiento",
        "🗄️ Historial y Consulta de Transacciones"
    ])

    # =========================================================
    # TAB 1: EXTRACTOR Y PROCESAMIENTO
    # =========================================================
    with tab_extractor:
        # Collapsible System Resources & Stage Timing Visor
        render_telemetry_expander(
            latest_telemetry=st.session_state.get("latest_telemetry"),
            default_expanded=st.session_state.get("telemetry_expanded", False)
        )

        uploaded_files = st.file_uploader(
            "Arrastra y suelta tus estados de cuenta o facturas (.pdf, .xml):",
            type=["pdf", "xml"],
            accept_multiple_files=True,
            help="Soporta múltiples archivos PDF y XML de cualquier banco o CFDI"
        )

        file_page_ranges = {}

        if uploaded_files:
            # Cache uploaded files to persistent temporary files
            if "cached_temp_files" not in st.session_state:
                st.session_state["cached_temp_files"] = {}

            cached_files = st.session_state["cached_temp_files"]

            # Compute SHA-256 hashes and check duplicate records in SQLite
            file_hashes = {}
            file_dup_info = {}

            for file in uploaded_files:
                file_bytes = file.getvalue()
                f_hash = compute_file_hash(file_bytes)
                file_hashes[file.name] = f_hash
                file_dup_info[file.name] = check_file_processed(file.name, f_hash)

                if file.name not in cached_files or not os.path.exists(cached_files[file.name]):
                    safe_name = "".join(c for c in file.name if c.isalnum() or c in "._- ")
                    tmp_path = os.path.join(tempfile.gettempdir(), f"stm_{safe_name}")
                    with open(tmp_path, "wb") as f_out:
                        f_out.write(file_bytes)
                    cached_files[file.name] = tmp_path

            # Duplicate Notice & Overwrite Toggle
            dups_found = [fname for fname, info in file_dup_info.items() if info is not None]
            if dups_found:
                st.warning(
                    f"⚠️ **Archivos previamente procesados ({len(dups_found)} de {len(uploaded_files)}):** "
                    f"`{', '.join(dups_found)}`. Para ahorrar recursos, estos archivos se omitirán a menos que actives la casilla inferior."
                )

            overwrite_duplicates = st.checkbox(
                "🔄 Reprocesar y sobrescribir archivos ya existentes",
                value=False,
                help="Si está desactivado, los archivos duplicados se omitirán sin invocar OCR ni Ollama. Si se activa, se eliminarán sus registros previos en la base de datos y se guardarán los nuevos."
            )

            # ---------------------------------------------------------
            # INDIVIDUAL EXTRACTION STRATEGIES & PAGE INSPECTION
            # ---------------------------------------------------------
            file_configs = {}

            # 1. XML Files Section
            xml_files = [f for f in uploaded_files if f.name.lower().endswith(".xml")]
            if xml_files:
                st.write("")
                st.subheader("📑 Facturas y Estados de Cuenta XML")
                st.caption("Configura individualmente la estrategia de extracción y metadatos para cada archivo XML.")

                for xml_f in xml_files:
                    dup_xml = file_dup_info.get(xml_f.name)
                    xml_badge = (
                        f"<span class='badge-duplicado'>🟡 Ya procesado el {dup_xml['fecha_procesamiento'][:10]} ({dup_xml['total_transacciones']} movs)</span>"
                        if dup_xml else
                        "<span class='badge-nuevo'>🟢 Nuevo (Listo para procesar)</span>"
                    )

                    with st.expander(f"📑 {xml_f.name} — Estrategia de Extracción XML", expanded=(len(uploaded_files) <= 4)):
                        st.markdown(f"**Estado:** {xml_badge}", unsafe_allow_html=True)
                        col_x1, col_x2 = st.columns(2)
                        with col_x1:
                            x_options = [
                                f"🌐 Usar Estrategia General ({xml_general_strategy.split(' (')[0]})",
                                "🎯 Máxima Precisión (Determinista Inteligente + Fallback Híbrido LLM)",
                                "⚡ Determinista Estricto (CFDI, Addenda, OFX, CAMT - 0s latencia)",
                                "🤖 Forzar Ollama LLM (Inferencia Generativa)"
                            ]
                            x_strat = st.selectbox(
                                f"Estrategia de Extracción ({xml_f.name}):",
                                options=x_options,
                                index=0,
                                key=f"xstrat_{xml_f.name}",
                                help="Por defecto hereda la estrategia general configurada. Puedes anularla para este archivo específico."
                            )
                        with col_x2:
                            x_rfc = st.text_input(
                                f"RFC del Titular ({xml_f.name}):",
                                value=user_rfc,
                                key=f"xrfc_{xml_f.name}",
                                help="RFC específico para clasificar Emisor como Ingreso y Receptor como Egreso (hereda el RFC global si se deja vacío)."
                            )

                        resolved_x = resolve_xml_strategy(x_strat, xml_general_strategy)
                        st.caption(f"Estrategia activa: **{resolved_x['strategy_label']}** (Modo: `{resolved_x['xml_mode']}`)")

                        file_configs[xml_f.name] = {
                            "type": "xml",
                            "strategy_label": resolved_x["strategy_label"],
                            "xml_mode": resolved_x["xml_mode"],
                            "user_rfc": x_rfc.strip().upper() if x_rfc and x_rfc.strip() else (user_rfc if user_rfc else None),
                            "model_name": model_name
                        }

            # 2. PDF Files Section
            pdf_files = [f for f in uploaded_files if f.name.lower().endswith(".pdf")]
            if pdf_files:
                st.write("")
                st.subheader("👁️ Vista Previa, Selección de Páginas y Estrategia por PDF")
                st.caption(
                    "Configura individualmente la estrategia de lectura (OCR, tablas, modelo) "
                    "y selecciona qué páginas procesar para cada estado de cuenta."
                )

                for pdf_f in pdf_files:
                    pdf_bytes = pdf_f.getvalue()
                    dup_pdf = file_dup_info.get(pdf_f.name)
                    dup_badge = (
                        f"<span class='badge-duplicado'>🟡 Ya procesado el {dup_pdf['fecha_procesamiento'][:10]} ({dup_pdf['total_transacciones']} movs)</span>"
                        if dup_pdf else
                        "<span class='badge-nuevo'>🟢 Nuevo (Listo para procesar)</span>"
                    )

                    try:
                        profile = detect_pdf_profile(pdf_bytes)
                        total_pages = profile["page_count"]
                        is_digital = profile["is_digital_native"]

                        badge_html = (
                            "<span class='badge-digital'>⚡ Digital Nativo (Vectorial)</span>"
                            if is_digital else
                            "<span class='badge-ocr'>🔍 Escaneado (Requiere OCR)</span>"
                        )

                        with st.expander(f"📄 {pdf_f.name} — {total_pages} página(s)", expanded=(len(uploaded_files) <= 4)):
                            st.markdown(
                                f"**Estado:** {dup_badge} | **Estructura:** {badge_html} | **Densidad:** `{profile['avg_chars_per_page']} caracteres/página`",
                                unsafe_allow_html=True
                            )

                            # Controls Row: Individual Strategy & Model
                            col_strat, col_mod = st.columns([3, 2])
                            with col_strat:
                                pdf_opts = [
                                    f"🌐 Usar Estrategia General ({pdf_general_strategy.split(' (')[0]})",
                                    "🎯 Máxima Precisión (Forzar RapidOCR + Accurate Tables)",
                                    "🤖 Automático (Detecta Digital vs Escaneo + Tablas Rápidas)",
                                    "⚡ Digital Nativo (10x Rápido - Omite OCR)",
                                    "🔍 Escaneo Físico (Forzar RapidOCR + Tablas Rápidas)",
                                    "🛠️ Personalizado (Ajustar OCR, escala y tablas)"
                                ]
                                strat_choice = st.selectbox(
                                    f"Estrategia de Extracción ({pdf_f.name}):",
                                    options=pdf_opts,
                                    index=0,
                                    key=f"strat_{pdf_f.name}",
                                    help="Por defecto hereda la estrategia general de PDFs configurada en la barra lateral. Puedes anularla para este archivo."
                                )
                            with col_mod:
                                avail_mods = st.session_state.get("available_models", [])
                                mod_opts = [f"Predeterminado ({model_name})"]
                                if avail_mods:
                                    mod_opts.extend([m for m in avail_mods if m != model_name])
                                mod_choice = st.selectbox(
                                    f"Modelo LLM ({pdf_f.name}):",
                                    options=mod_opts,
                                    index=0,
                                    key=f"model_{pdf_f.name}",
                                    help="Permite asignar un modelo de Ollama específico para este archivo."
                                )
                                eff_model = model_name if mod_choice.startswith("Predeterminado") else mod_choice

                            # Fine-tuning parameters if Personalizado
                            if strat_choice.startswith("🛠️ Personalizado"):
                                c_p1, c_p2, c_p3 = st.columns(3)
                                with c_p1:
                                    p_ocr_mode = st.selectbox(
                                        "Modo OCR",
                                        ["Automático", "Forzar OCR", "Sin OCR"],
                                        index=1,
                                        key=f"custom_ocr_{pdf_f.name}"
                                    )
                                    eff_ocr_custom = None if p_ocr_mode == "Automático" else (True if p_ocr_mode == "Forzar OCR" else False)
                                with c_p2:
                                    p_acc_mode = st.selectbox(
                                        "Estructura Tablas",
                                        ["Fast", "Accurate"],
                                        index=1,
                                        key=f"custom_tbl_{pdf_f.name}"
                                    )
                                    eff_acc_custom = (p_acc_mode == "Accurate")
                                with c_p3:
                                    eff_scale_custom = st.select_slider(
                                        "Escala OCR",
                                        options=[1.5, 2.0, 2.5, 3.0],
                                        value=2.5,
                                        key=f"custom_scale_{pdf_f.name}"
                                    )
                                resolved_pdf = resolve_pdf_strategy(
                                    strat_choice,
                                    pdf_general_strategy,
                                    custom_ocr=eff_ocr_custom,
                                    custom_accurate=eff_acc_custom,
                                    custom_scale=eff_scale_custom
                                )
                            else:
                                resolved_pdf = resolve_pdf_strategy(
                                    strat_choice,
                                    pdf_general_strategy
                                )

                            ocr_txt = "Forzado" if resolved_pdf["enable_ocr"] is True else ("Desactivado" if resolved_pdf["enable_ocr"] is False else "Auto")
                            tbl_txt = "Accurate" if resolved_pdf["accurate_tables"] else "Fast"
                            st.caption(f"Estrategia activa: **{resolved_pdf['strategy_label']}** (OCR: `{ocr_txt}` | Tablas: `{tbl_txt}` | Escala: `{resolved_pdf['ocr_scale']}x`)")

                            # Page Selection per File
                            st.markdown("##### 📄 Rango de Páginas a Procesar")
                            col_sel1, col_sel2 = st.columns(2)
                            with col_sel1:
                                start_p = st.number_input(
                                    f"Página Inicial ({pdf_f.name}):",
                                    min_value=1,
                                    max_value=total_pages,
                                    value=1,
                                    key=f"start_p_{pdf_f.name}"
                                )
                            with col_sel2:
                                end_p = st.number_input(
                                    f"Página Final ({pdf_f.name}):",
                                    min_value=1,
                                    max_value=total_pages,
                                    value=total_pages,
                                    key=f"end_p_{pdf_f.name}"
                                )

                            effective_range = (min(int(start_p), int(end_p)), max(int(start_p), int(end_p)))
                            file_page_ranges[pdf_f.name] = effective_range

                            file_configs[pdf_f.name] = {
                                "type": "pdf",
                                "strategy_label": resolved_pdf["strategy_label"],
                                "enable_ocr": resolved_pdf["enable_ocr"],
                                "accurate_tables": resolved_pdf["accurate_tables"],
                                "ocr_scale": resolved_pdf["ocr_scale"],
                                "model_name": eff_model,
                                "page_range": effective_range
                            }

                            # Interactive Page Viewer
                            st.markdown("##### 🔎 Visor de Páginas")
                            preview_col_ctrl, preview_col_img = st.columns([1, 2])

                            with preview_col_ctrl:
                                selected_preview = st.slider(
                                    "Selecciona la página a inspeccionar:",
                                    min_value=1,
                                    max_value=total_pages,
                                    value=effective_range[0],
                                    key=f"slider_preview_{pdf_f.name}"
                                )
                                st.caption(f"Visualizando página {selected_preview} de {total_pages}.")
                                show_grid = st.checkbox(
                                    "🖼️ Ver cuadrícula en miniatura de todas las páginas",
                                    value=False,
                                    key=f"grid_toggle_{pdf_f.name}"
                                )

                            with preview_col_img:
                                try:
                                    page_img = render_pdf_page_image(pdf_bytes, selected_preview, scale=0.85)
                                    st.image(
                                        page_img,
                                        caption=f"Página {selected_preview} de {total_pages} — {pdf_f.name}",
                                        width="stretch"
                                    )
                                except Exception as img_err:
                                    st.error(f"No se pudo renderizar la página {selected_preview}: {str(img_err)}")

                            # Optional Thumbnail Grid
                            if show_grid:
                                st.markdown("##### 🖼️ Cuadrícula de Páginas")
                                grid_cols = st.columns(min(total_pages, 4))
                                for p_num in range(1, total_pages + 1):
                                    col_target = grid_cols[(p_num - 1) % len(grid_cols)]
                                    try:
                                        thumb_img = render_pdf_page_image(pdf_bytes, p_num, scale=0.35)
                                        col_target.image(thumb_img, caption=f"Pág. {p_num}", width="stretch")
                                    except Exception:
                                        pass

                    except Exception as ex:
                        st.error(f"Error al inspeccionar {pdf_f.name}: {str(ex)}")
                        file_page_ranges[pdf_f.name] = (1, 1)
                        file_configs[pdf_f.name] = {
                            "type": "pdf",
                            "strategy_label": "Fallback",
                            "enable_ocr": enable_ocr_val,
                            "accurate_tables": accurate_mode,
                            "ocr_scale": ocr_scale,
                            "model_name": model_name,
                            "page_range": (1, 1)
                        }

            # Checkpoint detection for current batch of uploaded files
            batch_sig = ";".join([f"{f.name}:{len(f.getvalue())}" for f in uploaded_files])
            batch_id = compute_file_hash(batch_sig.encode())[:16]
            active_cp = get_batch_checkpoint(batch_id) or get_batch_checkpoint(None)

            # Validate that active_cp belongs to the currently uploaded files
            if active_cp:
                active_completed = set(active_cp.get("archivos_completados", []))
                active_failed = active_cp.get("archivo_fallido")
                uploaded_names = {f.name for f in uploaded_files}
                if not (active_completed.intersection(uploaded_names) or (active_failed and active_failed in uploaded_names)):
                    active_cp = None

            resume_btn = False
            skip_and_resume_btn = False

            # Display Interrupted Batch Recovery Card if active checkpoint exists
            if active_cp and active_cp.get("estado") == "interrupted":
                failed_name = active_cp.get("archivo_fallido", "Archivo")
                comp_count = len(active_cp.get("archivos_completados", []))
                tot_count = active_cp.get("archivos_totales", len(uploaded_files))
                err_msg = active_cp.get("ultimo_error", "Error no especificado")

                st.markdown(
                    f"""
                    <div style="background-color: #fff9db; border: 1px solid #f59f00; border-left: 6px solid #f59f00; border-radius: 8px; padding: 14px 18px; margin: 16px 0;">
                        <h4 style="margin: 0 0 6px 0; color: #d9480f;">⚠️ Lote Interrumpido por Error Detectado</h4>
                        <p style="margin: 0 0 6px 0; color: #495057; font-size: 0.95rem;">
                            El procesamiento se detuvo en <strong>{failed_name}</strong> tras completar con éxito <strong>{comp_count} de {tot_count}</strong> archivos (resguardados en la base de datos).
                        </p>
                        <div style="background: rgba(0,0,0,0.04); padding: 6px 10px; border-radius: 4px; font-family: monospace; font-size: 0.85rem; color: #c92a2a; margin-top: 6px;">
                            {err_msg}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                col_res, col_skip, col_discard = st.columns([2, 2, 1])
                with col_res:
                    resume_btn = st.button("🔄 Reanudar automáticamente donde se quedó", type="primary", width="stretch")
                with col_skip:
                    skip_and_resume_btn = st.button(f"⏭️ Omitir '{failed_name}' y continuar", width="stretch")
                with col_discard:
                    if st.button("🗑️ Descartar punto de control", width="stretch"):
                        clear_batch_checkpoint(batch_id)
                        clear_batch_checkpoint(None)
                        st.toast("Punto de control eliminado.", icon="🗑️")
                        st.rerun()

            st.write("")
            col_btn, col_clear = st.columns([2, 1])
            with col_btn:
                btn_label = "🚀 Iniciar Procesamiento Completo (Desde cero)" if (active_cp and active_cp.get("estado") == "interrupted") else "🚀 Procesar Estados de Cuenta"
                btn_type = "secondary" if (active_cp and active_cp.get("estado") == "interrupted") else "primary"
                process_btn = st.button(btn_label, type=btn_type, width="stretch")
            with col_clear:
                if st.button("🗑️ Limpiar Resultados", width="stretch"):
                    clear_batch_checkpoint(batch_id)
                    clear_batch_checkpoint(None)
                    st.session_state.pop("all_transactions", None)
                    st.session_state.pop("latest_telemetry", None)
                    st.session_state.pop("telemetry_expanded", None)
                    for p in st.session_state.get("cached_temp_files", {}).values():
                        if os.path.exists(p):
                            try:
                                os.unlink(p)
                            except Exception:
                                pass
                    st.session_state["cached_temp_files"] = {}
                    st.rerun()

            should_execute = process_btn or resume_btn or skip_and_resume_btn

            if should_execute:
                is_resuming = resume_btn or skip_and_resume_btn
                completed_files_set = set(active_cp.get("archivos_completados", [])) if (is_resuming and active_cp) else set()

                if skip_and_resume_btn and active_cp and active_cp.get("archivo_fallido"):
                    failed_target = active_cp["archivo_fallido"]
                    completed_files_set.add(failed_target)
                    mark_checkpoint_file_completed(batch_id, failed_target)
                    st.toast(f"Omitiendo archivo con fallo: {failed_target}", icon="⏭️")

                save_batch_checkpoint(
                    batch_id=batch_id,
                    total_files=len(uploaded_files),
                    completed_files=list(completed_files_set),
                    status="in_progress"
                )

                stopwatch = StageStopwatch()
                stopwatch.start_total()
                all_extracted: list[Transaccion] = []
                progress_bar = st.progress(0)
                status_text = st.empty()

                total_files = len(uploaded_files)
                skipped_count = 0
                has_interrupted = False

                for idx, file in enumerate(uploaded_files):
                    fname = file.name
                    ext = fname.lower().split(".")[-1]
                    f_hash = file_hashes.get(fname) or compute_file_hash(file.getvalue())
                    dup_record = file_dup_info.get(fname)

                    # Checkpoint Resumption check: If already completed in this batch, hydrate from DB
                    if is_resuming and fname in completed_files_set:
                        skipped_count += 1
                        status_text.info(f"⏭️ ({idx + 1}/{total_files}) **{fname}** ya completado en este lote. Cargando datos...")
                        progress_bar.progress((idx + 1) / total_files)

                        rec = check_file_processed(fname, f_hash)
                        if rec:
                            df_prev = query_transactions(archivo_id=rec["id"])
                            for _, r in df_prev.iterrows():
                                tipo_str = str(r["tipo"]).lower().strip()
                                tipo_enum = TipoTransaccion.INGRESO if tipo_str == "ingreso" else TipoTransaccion.EGRESO
                                all_extracted.append(Transaccion(
                                    fecha=str(r["fecha"])[:10],
                                    concepto=str(r["concepto"]),
                                    importe=float(r["importe"]),
                                    descripcion=str(r["descripcion"] or ""),
                                    tipo=tipo_enum
                                ))
                        continue

                    # Duplicate check: skip if previously processed in prior batches and not overwriting
                    if dup_record and not overwrite_duplicates and not is_resuming:
                        skipped_count += 1
                        st.toast(f"⏭️ Omitido {fname}: ya existe en BD ({dup_record['total_transacciones']} movs).", icon="ℹ️")
                        status_text.info(f"⏭️ ({idx + 1}/{total_files}) Omitiendo **{fname}** (Ya registrado previamente)...")
                        progress_bar.progress((idx + 1) / total_files)
                        mark_checkpoint_file_completed(batch_id, fname)
                        completed_files_set.add(fname)
                        continue

                    tmp_path = cached_files.get(fname)
                    if not tmp_path or not os.path.exists(tmp_path):
                        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                            tmp.write(file.getvalue())
                            tmp_path = tmp.name
                            cached_files[fname] = tmp_path

                    status_text.info(f"⏳ ({idx + 1}/{total_files}) Procesando archivo: **{fname}**...")
                    current_file_txs: list[Transaccion] = []

                    cfg = file_configs.get(fname, {})

                    try:
                        if ext == "xml":
                            # XML pipeline with individual strategy
                            xml_mode = cfg.get("xml_mode", "hybrid")
                            effective_rfc = cfg.get("user_rfc") or (user_rfc if user_rfc else None)
                            file_model = cfg.get("model_name", model_name)
                            strat_lbl = cfg.get("strategy_label", "🎯 Máxima Precisión")

                            if xml_mode == "llm":
                                status_text.info(f"⏳ ({idx + 1}/{total_files}) Procesando XML con {file_model} [{strat_lbl}]: **{fname}**...")
                                stopwatch.start_stage(f"🤖 Inferencia Ollama ({file_model})")
                                with open(tmp_path, "r", encoding="utf-8", errors="replace") as f_x:
                                    xml_raw = f_x.read()
                                llm_txs = extract_transactions_with_ollama(
                                    xml_raw,
                                    base_url=ollama_url,
                                    model=file_model,
                                    num_ctx=num_ctx
                                )
                                current_file_txs.extend(llm_txs)
                            else:
                                status_text.info(f"⏳ ({idx + 1}/{total_files}) Analizando XML [{strat_lbl}]: **{fname}**...")
                                stopwatch.start_stage("📄 Estructura XML / CFDI")
                                xml_txs, raw_text = parse_xml(tmp_path, user_rfc=effective_rfc)

                                if xml_txs is not None and len(xml_txs) > 0:
                                    current_file_txs.extend(xml_txs)
                                    st.toast(f"⚡ {fname}: {len(xml_txs)} movimientos extraídos [{strat_lbl}]", icon="📑")
                                elif raw_text and xml_mode in ("hybrid", "deterministic"):
                                    status_text.info(f"⏳ ({idx + 1}/{total_files}) Clasificando nodos XML con {file_model}...")
                                    stopwatch.start_stage(f"🤖 Inferencia Ollama ({file_model})")
                                    llm_txs = extract_transactions_with_ollama(
                                        raw_text,
                                        base_url=ollama_url,
                                        model=file_model,
                                        num_ctx=num_ctx
                                    )
                                    current_file_txs.extend(llm_txs)

                        elif ext == "pdf":
                            # PDF pipeline with individual strategy & page range
                            selected_range = cfg.get("page_range") or file_page_ranges.get(fname)
                            effective_ocr = cfg.get("enable_ocr", enable_ocr_val)
                            effective_accurate = cfg.get("accurate_tables", accurate_mode)
                            effective_scale = cfg.get("ocr_scale", ocr_scale)
                            file_model = cfg.get("model_name", model_name)
                            strat_lbl = cfg.get("strategy_label", "🎯 Máxima Precisión")

                            range_str = f"(Páginas {selected_range[0]} a {selected_range[1]})" if selected_range else ""
                            status_text.info(f"⏳ ({idx + 1}/{total_files}) Extrayendo tablas PDF de: **{fname}** {range_str} [{strat_lbl}]...")

                            stopwatch.start_stage("📑 Docling & RapidOCR")
                            markdown_tables, meta = parse_pdf(
                                tmp_path,
                                enable_ocr=effective_ocr,
                                accurate_tables=effective_accurate,
                                page_range=selected_range,
                                num_threads=num_threads,
                                ocr_scale=effective_scale,
                            )

                            st.toast(
                                f"⚡ {fname} [{strat_lbl}]: {meta['mode_label']} en {meta['elapsed_seconds']}s "
                                f"(Págs {meta['page_range'][0]}-{meta['page_range'][1]} de {meta['total_pages']})"
                            )

                            status_text.info(f"⏳ ({idx + 1}/{total_files}) Extrayendo y clasificando con {file_model}...")
                            stopwatch.start_stage(f"🤖 Inferencia Ollama ({file_model})")
                            pdf_txs = extract_transactions_with_ollama(
                                markdown_tables,
                                base_url=ollama_url,
                                model=file_model,
                                num_ctx=num_ctx
                            )
                            current_file_txs.extend(pdf_txs)

                        # Auto-save immediately to SQLite with persistent document retention
                        if current_file_txs:
                            save_processed_transactions(
                                filename=fname,
                                file_hash=f_hash,
                                file_type=ext,
                                transactions=current_file_txs,
                                file_bytes=file.getvalue(),
                                overwrite=overwrite_duplicates
                            )
                            mark_checkpoint_file_completed(batch_id, fname)
                            completed_files_set.add(fname)
                            st.toast(f"💾 Guardado en BD y almacenamiento: {len(current_file_txs)} movimientos de {fname}", icon="✅")
                            all_extracted.extend(current_file_txs)
                        else:
                            st.warning(f"⚠️ No se detectaron transacciones en {fname}.")
                            mark_checkpoint_file_completed(batch_id, fname)
                            completed_files_set.add(fname)

                    except Exception as err:
                        mark_checkpoint_interrupted(batch_id, fname, str(err))
                        st.error(f"❌ Error al procesar {fname}: {str(err)}")

                        if auto_pause_on_error:
                            st.warning(
                                f"💾 **Punto de control guardado automáticamente.** "
                                f"Se resguardaron {len(completed_files_set)} de {total_files} archivos en la base de datos. "
                                f"Puedes ajustar la estrategia de **{fname}** arriba o pulsar **'🔄 Reanudar automáticamente donde se quedó'** para continuar."
                            )
                            has_interrupted = True
                            break

                    progress_bar.progress((idx + 1) / total_files)

                stopwatch.start_stage("🧹 Normalización y Persistencia")
                stopwatch.finish_total()
                st.session_state["latest_telemetry"] = stopwatch.to_dict()
                st.session_state["telemetry_expanded"] = True

                if has_interrupted:
                    status_text.warning(f"⚠️ Procesamiento pausado en {fname}. Puedes reanudar donde se quedó cuando lo desees.")
                elif skipped_count > 0 and not all_extracted:
                    clear_batch_checkpoint(batch_id)
                    clear_batch_checkpoint(None)
                    status_text.warning(
                        f"Todos los {skipped_count} archivo(s) ya se encontraban registrados en la base de datos y fueron omitidos. "
                        "Puedes consultarlos en la pestaña '🗄️ Historial y Consulta de Transacciones' o marcar 'Reprocesar y sobrescribir'."
                    )
                else:
                    clear_batch_checkpoint(batch_id)
                    clear_batch_checkpoint(None)
                    status_text.success(f"✨ ¡Procesamiento completado! Se procesaron {len(all_extracted)} transacciones.")

                st.session_state["all_transactions"] = all_extracted

        # -----------------------------------------------------
        # RECENT RUN RESULTS VIEW
        # -----------------------------------------------------
        transactions = st.session_state.get("all_transactions", [])

        if transactions:
            df_all = create_standard_dataframe(transactions)
            df_ingresos = df_all[df_all["tipo"] == TipoTransaccion.INGRESO.value].copy()
            df_egresos = df_all[df_all["tipo"] == TipoTransaccion.EGRESO.value].copy()

            total_ingresos = df_ingresos["importe"].sum() if not df_ingresos.empty else 0.0
            total_egresos = df_egresos["importe"].sum() if not df_egresos.empty else 0.0
            balance_neto = total_ingresos - total_egresos

            st.write("")
            st.subheader("📊 Resumen del Lote Reciente")

            m1, m2, m3, m4 = st.columns(4)
            with m1:
                st.markdown(
                    f"<div class='metric-card'><div>💰 Total Ingresos</div><div class='metric-value-ingreso'>${total_ingresos:,.2f}</div><small>{len(df_ingresos)} movimientos</small></div>",
                    unsafe_allow_html=True
                )
            with m2:
                st.markdown(
                    f"<div class='metric-card'><div>💸 Total Egresos</div><div class='metric-value-egreso'>${total_egresos:,.2f}</div><small>{len(df_egresos)} movimientos</small></div>",
                    unsafe_allow_html=True
                )
            with m3:
                net_class = "metric-value-neto" if balance_neto >= 0 else "metric-value-egreso"
                st.markdown(
                    f"<div class='metric-card'><div>⚖️ Balance Neto</div><div class='{net_class}'>${balance_neto:,.2f}</div><small>Ingresos - Egresos</small></div>",
                    unsafe_allow_html=True
                )
            with m4:
                st.markdown(
                    f"<div class='metric-card'><div>📑 Transacciones Extraídas</div><div class='metric-value-neto'>{len(df_all)}</div><small>Lote actual</small></div>",
                    unsafe_allow_html=True
                )

            st.write("")

            # Global Excel Download button
            excel_bytes = df_to_excel_bytes(df_ingresos, df_egresos)
            st.download_button(
                label="📥 Descargar Libro Excel de esta Sesión (.xlsx con pestañas Ingresos y Egresos)",
                data=excel_bytes,
                file_name="estados_de_cuenta_sesion.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch"
            )

            # Document Inspector for recently processed batch
            with st.expander("👁️ Ver Estados de Cuenta Originales del Lote Reciente", expanded=False):
                recent_names = [f.name for f in uploaded_files] if uploaded_files else []
                matching_docs = [f for f in get_distinct_files() if f["nombre_archivo"] in recent_names]
                if matching_docs:
                    opts_docs = [f"{d['nombre_archivo']} (ID: {d['id']})" for d in matching_docs]
                    sel_doc_lbl = st.selectbox("Selecciona un documento para abrir en el visor:", options=opts_docs, key="recent_batch_doc_sel")
                    if sel_doc_lbl:
                        sel_doc_id = int(sel_doc_lbl.split("ID: ")[1].rstrip(")"))
                        sel_doc = next((d for d in matching_docs if d["id"] == sel_doc_id), None)
                        if sel_doc:
                            render_statement_inspector(sel_doc, key_prefix="recent_insp")
                else:
                    st.info("No se encontraron documentos físicos vinculados para este lote.")

            st.write("")

            tab_egresos, tab_ingresos, tab_todos = st.tabs([
                f"💸 Egresos / Gastos ({len(df_egresos)})",
                f"💰 Ingresos / Abonos ({len(df_ingresos)})",
                f"📊 Todos los Movimientos ({len(df_all)})"
            ])

            with tab_egresos:
                st.markdown("### Movimientos de Salida (Cargos, Comisiones, Retiros, Compras)")
                if not df_egresos.empty:
                    col_c1, col_c2 = st.columns([1, 1])
                    with col_c1:
                        tsv_egresos = df_to_tsv(df_egresos)
                        render_clipboard_button(tsv_egresos, "Copiar Egresos (Listo para Excel)", button_id="copy_egresos")
                    with col_c2:
                        csv_egresos = df_to_csv_bytes(df_egresos)
                        st.download_button(
                            "📥 Descargar Egresos (.csv)",
                            data=csv_egresos,
                            file_name="egresos.csv",
                            mime="text/csv",
                            key="dl_csv_egresos"
                        )

                    display_egresos = df_egresos.drop(columns=["tipo"], errors="ignore")
                    st.dataframe(
                        display_egresos,
                        width="stretch",
                        column_config={
                            "fecha": st.column_config.TextColumn("Fecha (YYYY-MM-DD)", width="medium"),
                            "concepto": st.column_config.TextColumn("Concepto", width="medium"),
                            "importe": st.column_config.NumberColumn("Importe", format="$%.2f", width="small"),
                            "descripción": st.column_config.TextColumn("Descripción Completa", width="large"),
                        },
                        hide_index=True
                    )
                else:
                    st.info("No se encontraron movimientos clasificados como egresos.")

            with tab_ingresos:
                st.markdown("### Movimientos de Entrada (Depósitos, Nómina, SPEI Recibidos, Rendimientos)")
                if not df_ingresos.empty:
                    col_i1, col_i2 = st.columns([1, 1])
                    with col_i1:
                        tsv_ingresos = df_to_tsv(df_ingresos)
                        render_clipboard_button(tsv_ingresos, "Copiar Ingresos (Listo para Excel)", button_id="copy_ingresos")
                    with col_i2:
                        csv_ingresos = df_to_csv_bytes(df_ingresos)
                        st.download_button(
                            "📥 Descargar Ingresos (.csv)",
                            data=csv_ingresos,
                            file_name="ingresos.csv",
                            mime="text/csv",
                            key="dl_csv_ingresos"
                        )

                    display_ingresos = df_ingresos.drop(columns=["tipo"], errors="ignore")
                    st.dataframe(
                        display_ingresos,
                        width="stretch",
                        column_config={
                            "fecha": st.column_config.TextColumn("Fecha (YYYY-MM-DD)", width="medium"),
                            "concepto": st.column_config.TextColumn("Concepto", width="medium"),
                            "importe": st.column_config.NumberColumn("Importe", format="$%.2f", width="small"),
                            "descripción": st.column_config.TextColumn("Descripción Completa", width="large"),
                        },
                        hide_index=True
                    )
                else:
                    st.info("No se encontraron movimientos clasificados como ingresos.")

            with tab_todos:
                st.markdown("### Vista Consolidada de Todas las Transacciones del Lote")
                if not df_all.empty:
                    col_t1, col_t2 = st.columns([1, 1])
                    with col_t1:
                        tsv_all = df_all.to_csv(sep="\t", index=False)
                        render_clipboard_button(tsv_all, "Copiar Todas las Transacciones", button_id="copy_all")
                    with col_t2:
                        csv_all = df_all.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
                        st.download_button(
                            "📥 Descargar Consolidado (.csv)",
                            data=csv_all,
                            file_name="todas_las_transacciones.csv",
                            mime="text/csv",
                            key="dl_csv_all"
                        )

                    st.dataframe(
                        df_all,
                        width="stretch",
                        column_config={
                            "fecha": st.column_config.TextColumn("Fecha (YYYY-MM-DD)", width="medium"),
                            "concepto": st.column_config.TextColumn("Concepto", width="medium"),
                            "importe": st.column_config.NumberColumn("Importe", format="$%.2f", width="small"),
                            "descripción": st.column_config.TextColumn("Descripción Completa", width="large"),
                            "tipo": st.column_config.TextColumn("Tipo (Ingreso/Egreso)", width="small"),
                        },
                        hide_index=True
                    )
        elif not uploaded_files:
            st.info("👆 Carga uno o más archivos de estado de cuenta (.pdf o .xml) y presiona **Procesar Estados de Cuenta** para comenzar.")

    # =========================================================
    # TAB 2: HISTORIAL Y CONSULTA DE TRANSACCIONES
    # =========================================================
    with tab_historial:
        st.subheader("🗄️ Historial Persistente de Transacciones")
        st.caption(
            "Consulta, filtra y exporta libremente todos los movimientos almacenados en tu base de datos SQLite local (`data/finanzas.db`)."
        )

        db_stats = get_database_stats()
        stored_files = get_distinct_files()

        if db_stats["total_transacciones"] == 0:
            st.info(
                "📭 **La base de datos local aún no tiene transacciones registradas.**\n\n"
                "Sube y procesa tus primeros estados de cuenta en la pestaña **'📥 Extractor y Procesamiento'**. "
                "Todas las transacciones se almacenarán automáticamente aquí para consultas futuras."
            )
        else:
            # -------------------------------------------------
            # FILTERS TOOLBAR
            # -------------------------------------------------
            with st.container(border=True):
                st.markdown("##### 🔍 Filtros de Búsqueda y Segmentación")

                col_f1, col_f2, col_f3, col_f4 = st.columns([2, 2, 2, 3])

                # Determine default min and max dates from database
                min_db_date = None
                max_db_date = None
                if db_stats.get("fecha_min"):
                    try:
                        min_db_date = datetime.strptime(db_stats["fecha_min"][:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
                if db_stats.get("fecha_max"):
                    try:
                        max_db_date = datetime.strptime(db_stats["fecha_max"][:10], "%Y-%m-%d").date()
                    except Exception:
                        pass

                with col_f1:
                    filter_fecha_desde = st.date_input(
                        "Fecha inicial:",
                        value=min_db_date,
                        key="hist_fecha_desde"
                    )
                with col_f2:
                    filter_fecha_hasta = st.date_input(
                        "Fecha final:",
                        value=max_db_date,
                        key="hist_fecha_hasta"
                    )
                with col_f3:
                    tipo_options = ["Todos los movimientos", "Solo Ingresos", "Solo Egresos"]
                    selected_tipo = st.selectbox(
                        "Tipo de flujo:",
                        options=tipo_options,
                        key="hist_tipo_select"
                    )
                with col_f4:
                    file_options = ["Todos los estados de cuenta"] + [
                        f"{f['nombre_archivo']} (ID: {f['id']})" for f in stored_files
                    ]
                    selected_file_label = st.selectbox(
                        "Estado de cuenta origen:",
                        options=file_options,
                        key="hist_file_select"
                    )

                search_keyword = st.text_input(
                    "Búsqueda por texto (comercio, concepto, referencia o folio):",
                    placeholder="Ej. Chedraui, SPEI, Amazon, Nómina, CFE...",
                    key="hist_search_keyword"
                )

            # Date Range Validation
            if filter_fecha_desde and filter_fecha_hasta and filter_fecha_desde > filter_fecha_hasta:
                st.warning("⚠️ La fecha inicial no puede ser posterior a la fecha final.")

            # Translate UI filters to database query parameters
            fecha_desde_str = filter_fecha_desde.strftime("%Y-%m-%d") if filter_fecha_desde else None
            fecha_hasta_str = filter_fecha_hasta.strftime("%Y-%m-%d") if filter_fecha_hasta else None

            tipo_filter = None
            if selected_tipo == "Solo Ingresos":
                tipo_filter = "ingreso"
            elif selected_tipo == "Solo Egresos":
                tipo_filter = "egreso"

            archivo_id_filter = None
            if selected_file_label != "Todos los estados de cuenta":
                try:
                    archivo_id_filter = int(selected_file_label.split("ID: ")[1].rstrip(")"))
                except Exception:
                    archivo_id_filter = None

            # Execute Filtered Query
            df_filtered = query_transactions(
                fecha_desde=fecha_desde_str,
                fecha_hasta=fecha_hasta_str,
                busqueda=search_keyword,
                tipo=tipo_filter,
                archivo_id=archivo_id_filter
            )

            # Statement Document Viewer if an individual statement is selected
            if archivo_id_filter:
                active_stmt = next((f for f in stored_files if f["id"] == archivo_id_filter), None)
                if active_stmt:
                    with st.expander(f"👁️ Ver Estado de Cuenta Origen: {active_stmt['nombre_archivo']}", expanded=True):
                        render_statement_inspector(active_stmt, key_prefix="hist_filter_insp")

            # -------------------------------------------------
            # DYNAMIC KPI CARDS
            # -------------------------------------------------
            df_hist_ingresos = df_filtered[df_filtered["tipo"] == "ingreso"].copy()
            df_hist_egresos = df_filtered[df_filtered["tipo"] == "egreso"].copy()

            hist_tot_ingresos = df_hist_ingresos["importe"].sum() if not df_hist_ingresos.empty else 0.0
            hist_tot_egresos = df_hist_egresos["importe"].sum() if not df_hist_egresos.empty else 0.0
            hist_balance_neto = hist_tot_ingresos - hist_tot_egresos

            st.write("")
            hm1, hm2, hm3, hm4 = st.columns(4)
            with hm1:
                st.markdown(
                    f"<div class='metric-card'><div>💰 Total Ingresos Filtrados</div>"
                    f"<div class='metric-value-ingreso'>${hist_tot_ingresos:,.2f}</div>"
                    f"<small>{len(df_hist_ingresos)} movimientos</small></div>",
                    unsafe_allow_html=True
                )
            with hm2:
                st.markdown(
                    f"<div class='metric-card'><div>💸 Total Egresos Filtrados</div>"
                    f"<div class='metric-value-egreso'>${hist_tot_egresos:,.2f}</div>"
                    f"<small>{len(df_hist_egresos)} movimientos</small></div>",
                    unsafe_allow_html=True
                )
            with hm3:
                h_net_class = "metric-value-neto" if hist_balance_neto >= 0 else "metric-value-egreso"
                st.markdown(
                    f"<div class='metric-card'><div>⚖️ Balance Neto Filtrado</div>"
                    f"<div class='{h_net_class}'>${hist_balance_neto:,.2f}</div>"
                    f"<small>Ingresos - Egresos</small></div>",
                    unsafe_allow_html=True
                )
            with hm4:
                st.markdown(
                    f"<div class='metric-card'><div>📑 Movimientos Filtrados</div>"
                    f"<div class='metric-value-neto'>{len(df_filtered)}</div>"
                    f"<small>de {db_stats['total_transacciones']} en total en BD</small></div>",
                    unsafe_allow_html=True
                )

            st.write("")

            # -------------------------------------------------
            # EXPORT ACTIONS TOOLBAR
            # -------------------------------------------------
            if not df_filtered.empty:
                exp_col1, exp_col2, exp_col3 = st.columns([2, 1, 1])

                with exp_col1:
                    excel_filtered_bytes = df_to_excel_bytes(df_hist_ingresos, df_hist_egresos)
                    st.download_button(
                        label="📥 Descargar Excel Filtrado (.xlsx)",
                        data=excel_filtered_bytes,
                        file_name="historial_transacciones_filtradas.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        width="stretch",
                        key="btn_dl_hist_excel"
                    )

                with exp_col2:
                    csv_export_df = df_filtered.drop(columns=["id", "archivo_id"], errors="ignore")
                    csv_filtered_bytes = df_to_csv_bytes(csv_export_df)
                    st.download_button(
                        label="📥 Descargar CSV Filtrado (.csv)",
                        data=csv_filtered_bytes,
                        file_name="historial_transacciones_filtradas.csv",
                        mime="text/csv",
                        width="stretch",
                        key="btn_dl_hist_csv"
                    )

                with exp_col3:
                    tsv_export_df = df_filtered.drop(columns=["id", "archivo_id"], errors="ignore")
                    tsv_filtered_str = df_to_tsv(tsv_export_df)
                    render_clipboard_button(
                        tsv_filtered_str,
                        label="Copiar Filtrado",
                        button_id="copy_hist_tsv"
                    )

                # -------------------------------------------------
                # FILTERED TRANSACTIONS TABLE
                # -------------------------------------------------
                display_df = df_filtered.drop(columns=["id", "archivo_id"], errors="ignore")
                st.dataframe(
                    display_df,
                    width="stretch",
                    column_config={
                        "fecha": st.column_config.TextColumn("Fecha (YYYY-MM-DD)", width="small"),
                        "concepto": st.column_config.TextColumn("Concepto", width="medium"),
                        "importe": st.column_config.NumberColumn("Importe", format="$%.2f", width="small"),
                        "descripción": st.column_config.TextColumn("Descripción Completa", width="large"),
                        "tipo": st.column_config.TextColumn("Tipo", width="small"),
                        "nombre_archivo": st.column_config.TextColumn("Estado de Cuenta", width="medium"),
                    },
                    hide_index=True
                )
            else:
                st.info("🔍 No se encontraron transacciones que coincidan con los filtros aplicados.")

            st.write("")

            # -------------------------------------------------
            # AUDIT & FILE MANAGEMENT EXPANDER
            # -------------------------------------------------
            with st.expander("📁 Gestión de Estados de Cuenta Almacenados (Auditoría y Purga)", expanded=False):
                st.markdown("##### Estados de Cuenta Registrados")
                st.caption(
                    "Listado de archivos cargados. Si un estado de cuenta fue procesado por error, "
                    "puedes eliminarlo y todas sus transacciones asociadas serán purgadas automáticamente."
                )

                if stored_files:
                    df_files = pd.DataFrame(stored_files)
                    # Format size if present
                    if "tamano_bytes" in df_files.columns:
                        df_files["tamaño"] = df_files["tamano_bytes"].apply(
                            lambda b: f"{b/1024:.1f} KB" if pd.notnull(b) and b < 1024*1024 else (f"{b/(1024*1024):.2f} MB" if pd.notnull(b) else "N/D")
                        )
                    else:
                        df_files["tamaño"] = "N/D"

                    cols_to_show = [
                        "id", "nombre_archivo", "tipo_archivo", "tamaño", "fecha_procesamiento",
                        "total_transacciones", "rango_fecha_inicio", "rango_fecha_fin"
                    ]
                    available_cols = [c for c in cols_to_show if c in df_files.columns]
                    display_files = df_files[available_cols].copy()

                    st.dataframe(
                        display_files,
                        width="stretch",
                        column_config={
                            "id": st.column_config.NumberColumn("ID", width="small"),
                            "nombre_archivo": st.column_config.TextColumn("Nombre Archivo", width="medium"),
                            "tipo_archivo": st.column_config.TextColumn("Tipo", width="small"),
                            "tamaño": st.column_config.TextColumn("Tamaño", width="small"),
                            "fecha_procesamiento": st.column_config.TextColumn("Fecha Procesamiento", width="medium"),
                            "total_transacciones": st.column_config.NumberColumn("Transacciones", width="small"),
                            "rango_fecha_inicio": st.column_config.TextColumn("Desde", width="small"),
                            "rango_fecha_fin": st.column_config.TextColumn("Hasta", width="small"),
                        },
                        hide_index=True
                    )

                    st.divider()
                    st.markdown("##### 👁️ Visor y Descarga de Estados de Cuenta Almacenados")
                    st.caption("Abre, revisa páginas o descarga cualquier estado de cuenta físico preservado en `data/documentos/`.")
                    audit_view_options = [
                        f"{f['nombre_archivo']} (ID: {f['id']} - {f['total_transacciones']} movs)"
                        for f in stored_files
                    ]
                    target_audit_view_lbl = st.selectbox(
                        "Selecciona un estado de cuenta para abrir en el visor:",
                        options=audit_view_options,
                        key="audit_view_select"
                    )
                    if target_audit_view_lbl:
                        v_id = int(target_audit_view_lbl.split("ID: ")[1].split(" -")[0])
                        file_to_view = next((f for f in stored_files if f["id"] == v_id), None)
                        if file_to_view:
                            render_statement_inspector(file_to_view, key_prefix="audit_mgmt_insp")

                    st.divider()
                    st.markdown("##### 🗑️ Purgar Estado de Cuenta")
                    del_options = [
                        f"{f['nombre_archivo']} (ID: {f['id']} - {f['total_transacciones']} movs)"
                        for f in stored_files
                    ]
                    target_to_del_label = st.selectbox(
                        "Selecciona el estado de cuenta a eliminar:",
                        options=del_options,
                        key="del_file_select"
                    )
                    confirm_delete = st.checkbox(
                        "Confirmo que deseo eliminar este estado de cuenta y todas sus transacciones asociadas en cascada.",
                        value=False,
                        key="confirm_del_check"
                    )

                    if st.button("🗑️ Eliminar Archivo Seleccionado", type="secondary", disabled=not confirm_delete):
                        try:
                            target_id = int(target_to_del_label.split("ID: ")[1].split(" -")[0])
                            success = delete_processed_file(target_id)
                            if success:
                                st.success("✅ Estado de cuenta y sus transacciones eliminados exitosamente.")
                                st.rerun()
                            else:
                                st.error("❌ No se encontró el archivo especificado en la base de datos.")
                        except Exception as del_err:
                            st.error(f"❌ Error al eliminar el archivo: {str(del_err)}")


if __name__ == "__main__":
    main()
