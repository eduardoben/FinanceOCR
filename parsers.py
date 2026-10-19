"""
parsers.py - Document and XML Parsers for Bank Statements

Provides layout-aware PDF parsing via Docling (OCR & TableFormer)
with intelligent hybrid pre-flight inspection (pypdfium2),
singleton converter caching, dynamic multithreading, and
deterministic Mexican CFDI / XML parsing via lxml.
"""

import os
import re
import time
import functools
import threading
import multiprocessing
from typing import List, Tuple, Optional, Dict, Any
from PIL import Image
from lxml import etree
import pypdfium2 as pdfium

from models import Transaccion, TipoTransaccion, sanitize_importe, compute_row_confidence
from date_utils import normalize_spanish_date, extract_statement_year, extract_statement_period

# Docling imports
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableFormerMode,
    AcceleratorOptions,
    RapidOcrOptions,
)
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend

# Global converter instance registry to eliminate cold-start overhead (thread-safe singleton)
_CONVERTERS_CACHE: Dict[Tuple[bool, bool, int, float], DocumentConverter] = {}
_CONVERTER_LOCK = threading.Lock()

# Precompiled word-boundary regular expressions for Mexican CFDI financial flow classification
EXPENSE_KEYWORDS = [
    "CGO", "CARGO", "RETIRO", "DISPOSICION", "CAJERO",
    "PAGO DE TARJETA", "PAGO TARJETA", "PAGO SERVICIO", "PAGO DE SERVICIOS",
    "PORTABILIDAD", "PORTABILIDAD DE NOMINA",
    "TRANSFERENCIA A", "TRANSFERENCIA INTERBANCARIA", "SPEI ENVIADO",
    "TRASPASO A", "COMPRA", "CONSUMO", "COMISION", "IVA", "VALIDACION"
]
INCOME_KEYWORDS = [
    "SU PAGO GRACIAS", "PAGO GRACIAS", "SU PAGO",
    "NOMINA", "SUELDO", "HONORARIOS",
    "PAGO DE BECA", "DEPOSITO BECA", "BECA",
    "HACIENDA TE DEVUELVE", "DEVOLUCION", "REEMBOLSO",
    "BONIFICACION", "CASHBACK", "2NOW", "RECOMPENSA", "SALDO A FAVOR",
    "ABONO", "ABO", "DEPOSITO", "DEP",
    "SPEI RECIBIDO", "TRANSFERENCIA RECIBIDA", "TRASPASO RECIBIDO",
    "INTERESES GANADOS", "RENDIMIENTOS"
]

EXPENSE_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(k.strip()) for k in sorted(EXPENSE_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE
)
INCOME_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(k.strip()) for k in sorted(INCOME_KEYWORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE
)


def detect_pdf_profile(
    file_input: Any,
    char_threshold_per_page: float = 60.0
) -> Dict[str, Any]:
    """
    Rapidly evaluates the PDF document structure in milliseconds (<10ms)
    using pypdfium2 to determine whether it possesses a native digital vector
    text layer or represents a scanned / rasterized document requiring heavy OCR.

    Args:
        file_input: File path (str) or raw binary data (bytes).
        char_threshold_per_page: Average character density threshold.

    Returns:
        Dictionary with page_count, char_counts, avg_chars_per_page,
        is_digital_native, and recommended_mode ('digital' or 'ocr').
    """
    try:
        if isinstance(file_input, str):
            if not os.path.exists(file_input):
                raise FileNotFoundError(f"No se encontró el archivo PDF: {file_input}")
            try:
                doc = pdfium.PdfDocument(file_input)
            except Exception:
                # Fallback to loading via in-memory bytes if file handle is locked
                with open(file_input, "rb") as f:
                    doc = pdfium.PdfDocument(f.read())
        else:
            doc = pdfium.PdfDocument(file_input)

        total_pages = len(doc)
        char_counts: List[int] = []

        for page in doc:
            tp = page.get_textpage()
            text = tp.get_text_range()
            char_counts.append(len(text.strip()))

        avg_chars = sum(char_counts) / max(total_pages, 1)
        is_digital = avg_chars >= char_threshold_per_page
        has_scanned = any(c < char_threshold_per_page for c in char_counts)

        return {
            "page_count": total_pages,
            "char_counts": char_counts,
            "avg_chars_per_page": round(avg_chars, 1),
            "is_digital_native": is_digital,
            "has_scanned_pages": has_scanned,
            "recommended_mode": "digital" if is_digital else "ocr",
        }
    except Exception as ex:
        # Graceful fallback for protected or non-standard PDF formats
        return {
            "page_count": 1,
            "char_counts": [0],
            "avg_chars_per_page": 0.0,
            "is_digital_native": False,
            "has_scanned_pages": True,
            "recommended_mode": "ocr",
            "warning": str(ex),
        }



def render_pdf_page_image(
    file_input: Any,
    page_number: int,
    scale: float = 1.0
) -> Image.Image:
    """
    Renders an individual PDF page into a PIL Image for UI visual inspection
    and thumbnail preview, allowing users to accurately select page ranges.

    Args:
        file_input: File path (str) or raw binary data (bytes).
        page_number: 1-indexed page number.
        scale: Resolution scale factor (1.0 = ~72 DPI standard screen preview).

    Returns:
        PIL Image of the rendered page.
    """
    if isinstance(file_input, str):
        if not os.path.exists(file_input):
            raise FileNotFoundError(f"No se encontró el archivo PDF: {file_input}")
        try:
            doc = pdfium.PdfDocument(file_input)
        except Exception:
            with open(file_input, "rb") as f:
                doc = pdfium.PdfDocument(f.read())
    else:
        doc = pdfium.PdfDocument(file_input)

    total_pages = len(doc)
    if page_number < 1 or page_number > total_pages:
        raise ValueError(f"Página {page_number} fuera de rango. El documento tiene {total_pages} páginas.")

    page = doc[page_number - 1]
    return page.render(scale=scale).to_pil()


def get_cached_converter(
    enable_ocr: bool = False,
    accurate_tables: bool = False,
    num_threads: Optional[int] = None,
    ocr_scale: float = 2.0,
) -> DocumentConverter:
    """
    Retrieves or creates a cached DocumentConverter instance with tailored
    accelerator and OCR pipeline options in a thread-safe manner, preventing
    repeated model initialization.
    """
    if num_threads is None:
        cpu_cores = multiprocessing.cpu_count() or 4
        num_threads = min(cpu_cores, 12)

    cache_key = (enable_ocr, accurate_tables, num_threads, ocr_scale)
    with _CONVERTER_LOCK:
        if cache_key in _CONVERTERS_CACHE:
            return _CONVERTERS_CACHE[cache_key]

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = enable_ocr
        pipeline_options.do_table_structure = True
        pipeline_options.table_structure_options.mode = (
            TableFormerMode.ACCURATE if accurate_tables else TableFormerMode.FAST
        )
        pipeline_options.accelerator_options = AcceleratorOptions(
            num_threads=num_threads,
            device="auto"
        )

        if enable_ocr:
            pipeline_options.ocr_options = RapidOcrOptions(
                scale=ocr_scale,
                backend="onnxruntime"
            )

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=pipeline_options,
                    backend=PyPdfiumDocumentBackend,
                )
            }
        )

        _CONVERTERS_CACHE[cache_key] = converter
        return converter


def parse_pdf(
    file_path: str,
    enable_ocr: Optional[bool] = None,
    accurate_tables: bool = False,
    page_range: Optional[Tuple[int, int]] = None,
    num_threads: Optional[int] = None,
    ocr_scale: float = 2.0,
) -> Tuple[str, Dict[str, Any]]:
    """
    Parses a PDF bank statement (scanned image or digital native text)
    using an optimized Docling pipeline with automatic mode routing.

    Args:
        file_path: Absolute or relative path to the PDF file.
        enable_ocr:
            - None: 'auto' mode. Pre-flight heuristic checks text density.
            - True: Force OCR (for scanned pages / image PDFs).
            - False: Force digital native text extraction (bypasses OCR).
        accurate_tables: Use TableFormer ACCURATE mode (True) or FAST mode (False).
        page_range: Optional (start_page, end_page) 1-indexed tuple.
        num_threads: CPU concurrency threads for deep learning execution.
        ocr_scale: Image scaling factor for OCR (default 2.0 is fast and crisp).

    Returns:
        Tuple of (markdown_output: str, metadata: dict)
        where metadata contains processing time, effective mode, page range, and stats.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"No se encontró el archivo PDF: {file_path}")

    start_time = time.time()
    profile = detect_pdf_profile(file_path)
    total_pages = profile["page_count"]

    # Validate and normalize page_range
    effective_page_range: Optional[Tuple[int, int]] = None
    if page_range:
        p_start = max(1, min(page_range[0], total_pages))
        p_end = max(p_start, min(page_range[1], total_pages))
        effective_page_range = (p_start, p_end)

    # Determine effective OCR mode
    if enable_ocr is None:
        # Inspect pages within effective_page_range (or all pages if no range is specified):
        # if any page has fewer characters than the threshold (60.0), activate OCR
        pages_to_check = (
            profile["char_counts"][effective_page_range[0]-1 : effective_page_range[1]]
            if effective_page_range
            else profile["char_counts"]
        )
        effective_ocr = any(c < 60.0 for c in pages_to_check)
        detection_reason = "auto_detected"
    else:
        effective_ocr = bool(enable_ocr)
        detection_reason = "user_override"


    converter = get_cached_converter(
        enable_ocr=effective_ocr,
        accurate_tables=accurate_tables,
        num_threads=num_threads,
        ocr_scale=ocr_scale,
    )

    # Convert with Docling
    if effective_page_range:
        result = converter.convert(file_path, page_range=effective_page_range)
        pages_processed = effective_page_range[1] - effective_page_range[0] + 1
    else:
        result = converter.convert(file_path)
        pages_processed = total_pages

    markdown_output = result.document.export_to_markdown()
    elapsed_seconds = round(time.time() - start_time, 2)

    metadata = {
        "elapsed_seconds": elapsed_seconds,
        "effective_ocr": effective_ocr,
        "mode_label": "OCR (RapidOCR)" if effective_ocr else "Digital Nativo (Vectorial)",
        "detection_reason": detection_reason,
        "is_digital_native": profile["is_digital_native"],
        "pages_processed": pages_processed,
        "total_pages": total_pages,
        "page_range": effective_page_range or (1, total_pages),
        "accurate_tables": accurate_tables,
    }

    return markdown_output, metadata



import re

DATE_PATTERN = re.compile(
    r"\b(?:"
    r"\d{4}[-/\s]\d{1,2}[-/\s]\d{1,2}"  # YYYY-MM-DD
    r"|\d{1,2}[-/\s](?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|[a-z]{3})(?:[-/\s]\d{2,4})?"  # 15 ENE, 15/ENE, 15-ENE, 15/ENE/2024
    r"|\d{1,2}[-/\s]\d{1,2}[-/\s]\d{2,4}"  # 15/01/2024, 15-01-24
    r"|\d{1,2}[-/]\d{1,2}"  # 15/01, 15-01
    r"|\d{8}"  # 20240115
    r")\b",
    re.IGNORECASE
)
MONEY_PATTERN = re.compile(r"(?:\$\s*[\d,]+\.\d{2}|[\d,]+\.\d{2})")


def is_candidate_row(line: str) -> bool:
    """
    Evaluates whether a text or table row is a candidate financial transaction row.
    """
    stripped = line.strip()
    return bool(DATE_PATTERN.search(stripped) and MONEY_PATTERN.search(stripped))


def evaluar_linea_candidata(line: str) -> Dict[str, Any]:
    """
    Returns candidate status evaluation dictionary: {"candidata": bool, "linea": str}
    """
    return {
        "candidata": is_candidate_row(line),
        "linea": line
    }


def parse_xml(file_path: str, user_rfc: Optional[str] = None) -> Tuple[Optional[List[Transaccion]], Optional[str]]:
    """
    Parses an XML file with multi-level inspection and deterministic extraction:
    1. Mexican CFDI (v3.2, v3.3, v4.0) single or batch files:
       - Sub-level Complemento Estado de Cuenta Bancario (ecb:EstadoDeCuentaBancario) -> all bank movements.
       - Sub-level Complemento de Recepción de Pagos (pago10/pago20:Pagos) -> payments and related docs.
       - Sub-level Complemento Estado de Cuenta Combustible (ecc11/ecc12) -> fuel charges.
       - Sub-level Complemento Nómina (nomina12:Nomina) -> payroll.
       - Standard line items (cfdi:Concepto) with tax calculation (IVA/retenciones) to avoid partial amounts.
       - UUID extraction from TimbreFiscalDigital.
    2. Open Financial Exchange (OFX / QFX) banking XML -> deterministic statement movements.
    3. ISO 20022 CAMT (camt.052, camt.053, camt.054) XML -> deterministic entry movements.
    4. Excel SpreadsheetML XML (urn:schemas-microsoft-com:office:spreadsheet) -> tabular rows.
    5. Generic Banking XML with repeating transaction/movement nodes -> deterministic or Markdown table.
    6. Generic or ambiguous XML -> Hierarchical structured text preserving all parent-child levels.

    Returns:
        (transactions, None) if parsed deterministically.
        (None, raw_text_or_markdown) if ambiguous or generic XML requiring LLM extraction.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"No se encontró el archivo XML: {file_path}")

    parser = etree.XMLParser(recover=True, remove_comments=True, resolve_entities=False, no_network=True)
    try:
        tree = etree.parse(file_path, parser=parser)

        root = tree.getroot()
    except Exception:
        # Fallback for encoding anomalies (Latin-1, UTF-16, etc.)
        with open(file_path, "rb") as f:
            raw_bytes = f.read()
        root = etree.fromstring(raw_bytes, parser=parser)

    if root is None:
        return None, ""

    nsmap = root.nsmap.copy()
    root_tag_low = root.tag.lower()

    # 1. Mexican CFDI check (root or nested Comprobante nodes)
    is_cfdi = any("sat.gob.mx" in str(uri) for uri in nsmap.values()) or "comprobante" in root_tag_low
    if not is_cfdi:
        is_cfdi = bool(root.xpath(".//*[local-name()='Comprobante']"))

    if is_cfdi:
        txs = _parse_all_cfdi(root, user_rfc)
        if txs:
            return txs, None

    # 2. OFX / QFX Banking XML check
    is_ofx = "ofx" in root_tag_low or bool(root.xpath(".//*[translate(local-name(), 'stmttrn', 'STMTTRN')='STMTTRN']"))
    if is_ofx:
        txs = _parse_ofx_xml(root)
        if txs:
            return txs, None

    # 3. CAMT.053 / ISO 20022 XML check
    is_camt = (
        any("camt" in str(uri) for uri in nsmap.values())
        or bool(root.xpath(".//*[local-name()='BkToCstmrStmt']"))
        or bool(root.xpath(".//*[local-name()='Ntry']"))
    )
    if is_camt:
        txs = _parse_camt_xml(root)
        if txs:
            return txs, None

    # 4. Excel XML SpreadsheetML check
    is_excel_xml = "workbook" in root_tag_low or any("spreadsheet" in str(uri).lower() for uri in nsmap.values())
    if is_excel_xml:
        return _parse_excel_xml(root)

    # 5. Generic banking XML with repeating transaction/movement nodes
    gen_txs, gen_md = _parse_generic_financial_xml(root)
    if gen_txs:
        return gen_txs, None
    if gen_md:
        return None, gen_md

    # 6. Fallback to clean hierarchical text preserving all levels
    return None, _extract_hierarchical_xml_text(root)


def _parse_all_cfdi(root: etree._Element, user_rfc: Optional[str] = None) -> List[Transaccion]:
    """
    Extracts transactions from one or multiple CFDI Comprobante nodes in an XML file.
    Supports single invoices, credit notes, payment receipts, payroll, bank statements,
    and batch XML containers containing multiple Comprobantes.
    """
    comprobantes = root.xpath(".//*[local-name()='Comprobante']")
    if not comprobantes and "comprobante" in root.tag.lower():
        comprobantes = [root]
    if not comprobantes:
        comprobantes = [root]

    all_txs: List[Transaccion] = []
    for comp in comprobantes:
        all_txs.extend(_parse_single_cfdi(comp, user_rfc))
    return all_txs


def _parse_single_cfdi(comp: etree._Element, user_rfc: Optional[str] = None) -> List[Transaccion]:
    """
    Deterministic CFDI extractor with deep complement inspection:
    1. Complemento Estado de Cuenta Bancario (ecb:EstadoDeCuentaBancario)
    2. Complemento de Recepción de Pagos (pago10 / pago20:Pagos)
    3. Complemento Estado de Cuenta Combustible (ecc11 / ecc12)
    4. Complemento Nómina (nomina12:Nomina)
    5. Standard line items (cfdi:Concepto) with full tax incorporation and UUID tracking.
    """
    fecha_raw = comp.get("Fecha") or comp.get("fecha") or ""
    fecha = normalize_spanish_date(fecha_raw) if fecha_raw else "2024-01-01"

    tipo_comprobante = (comp.get("TipoDeComprobante") or comp.get("tipoDeComprobante") or "I").upper()
    total_str = comp.get("Total") or comp.get("total") or "0"
    subtotal_str = comp.get("SubTotal") or comp.get("subTotal") or "0"

    try:
        total = abs(float(total_str.replace(",", "").strip()))
    except ValueError:
        total = 0.0

    try:
        subtotal = abs(float(subtotal_str.replace(",", "").strip()))
    except ValueError:
        subtotal = 0.0

    folio = comp.get("Folio") or comp.get("folio") or ""
    serie = comp.get("Serie") or comp.get("serie") or ""
    folio_completo = f"{serie}-{folio}".strip("-") if (serie or folio) else "S/F"

    moneda = (comp.get("Moneda") or comp.get("moneda") or "MXN").strip()
    tc_raw = (comp.get("TipoCambio") or comp.get("tipoCambio") or "0").strip()
    try:
        tipo_cambio = float(tc_raw.replace(",", ""))
    except ValueError:
        tipo_cambio = 0.0

    # UUID from TimbreFiscalDigital

    uuid_elems = comp.xpath(".//*[local-name()='TimbreFiscalDigital']")
    uuid = uuid_elems[0].get("UUID") or "" if uuid_elems else ""
    if folio_completo == "S/F" and uuid:
        folio_completo = f"UUID:{uuid[:8]}"

    # Emisor
    emisor_elem = comp.xpath(".//*[local-name()='Emisor']")
    emisor_nombre = "Emisor Desconocido"
    emisor_rfc = ""
    if emisor_elem:
        emisor_nombre = emisor_elem[0].get("Nombre") or emisor_elem[0].get("Rfc") or "Emisor Desconocido"
        emisor_rfc = emisor_elem[0].get("Rfc") or ""

    # Receptor
    receptor_elem = comp.xpath(".//*[local-name()='Receptor']")
    receptor_rfc = receptor_elem[0].get("Rfc") or "" if receptor_elem else ""

    # Flow determination
    if user_rfc:
        user_clean = user_rfc.strip().upper()
        if user_clean == emisor_rfc.upper():
            default_tipo = TipoTransaccion.INGRESO
        elif user_clean == receptor_rfc.upper():
            default_tipo = TipoTransaccion.EGRESO
        else:
            default_tipo = TipoTransaccion.EGRESO
    else:
        if tipo_comprobante == "E":
            default_tipo = TipoTransaccion.INGRESO
        elif tipo_comprobante == "N":
            default_tipo = TipoTransaccion.INGRESO
        else:
            default_tipo = TipoTransaccion.EGRESO

    txs: List[Transaccion] = []

    # -------------------------------------------------------------
    # SUB-LEVEL COMPLEMENT 1: EstadoDeCuentaBancario (ecb)
    # -------------------------------------------------------------
    ecb_movs = comp.xpath(".//*[local-name()='EstadoDeCuentaBancario']//*[local-name()='Movimiento']")
    if ecb_movs:
        for m in ecb_movs:
            m_f = normalize_spanish_date(m.get("fecha") or m.get("Fecha") or fecha)
            m_desc = m.get("descripcion") or m.get("Descripcion") or "Movimiento bancario"
            m_imp_str = m.get("importe") or m.get("Importe") or "0"
            try:
                m_imp = abs(float(m_imp_str.replace(",", "").strip()))
            except ValueError:
                m_imp = 0.0

            m_tipo_raw = (m.get("tipo") or m.get("Tipo") or "").upper()
            if "ABONO" in m_tipo_raw or "DEPOSITO" in m_tipo_raw:
                m_flow = TipoTransaccion.INGRESO
            elif "CARGO" in m_tipo_raw or "RETIRO" in m_tipo_raw:
                m_flow = TipoTransaccion.EGRESO
            else:
                m_flow = default_tipo

            m_ref = m.get("referencia") or m.get("Referencia") or ""
            extra = f"{m_desc} | Banco: {emisor_nombre} ({emisor_rfc})"
            if m_ref:
                extra += f" | Ref: {m_ref}"
            if uuid:
                extra += f" | UUID: {uuid}"

            txs.append(
                Transaccion(
                    fecha=m_f,
                    concepto=emisor_nombre,
                    importe=m_imp,
                    descripcion=extra,
                    tipo=m_flow
                )
            )
        return txs

    # -------------------------------------------------------------
    # SUB-LEVEL COMPLEMENT 2: Recepción de Pagos (pago10 / pago20)
    # -------------------------------------------------------------
    pagos = comp.xpath(".//*[local-name()='Pagos']//*[local-name()='Pago']")
    if pagos:
        for p in pagos:
            p_f = normalize_spanish_date(p.get("FechaPago") or p.get("fechaPago") or fecha)
            p_monto_str = p.get("Monto") or p.get("monto") or "0"
            try:
                p_monto = abs(float(p_monto_str.replace(",", "").strip()))
            except ValueError:
                p_monto = 0.0

            p_op = p.get("NumOperacion") or p.get("numOperacion") or ""
            doctos = p.xpath(".//*[local-name()='DoctoRelacionado']")
            if doctos and len(doctos) > 1:
                for d in doctos:
                    d_uuid = d.get("IdDocumento") or ""
                    d_folio = d.get("Folio") or ""
                    d_imp_str = d.get("ImpPagado") or str(p_monto)
                    try:
                        d_imp = abs(float(d_imp_str.replace(",", "").strip()))
                    except ValueError:
                        d_imp = p_monto
                    extra = f"Pago CFDI | Docto: {d_folio or d_uuid[:8]} | Emisor: {emisor_nombre} ({emisor_rfc})"
                    if p_op:
                        extra += f" | Op: {p_op}"
                    if uuid:
                        extra += f" | UUID REP: {uuid}"
                    txs.append(
                        Transaccion(
                            fecha=p_f,
                            concepto=emisor_nombre,
                            importe=d_imp,
                            descripcion=extra,
                            tipo=default_tipo
                        )
                    )
            else:
                d_uuid = doctos[0].get("IdDocumento") or "" if doctos else ""
                extra = f"Complemento de Pago | Emisor: {emisor_nombre} ({emisor_rfc})"
                if d_uuid:
                    extra += f" | Docto: {d_uuid[:8]}"
                if p_op:
                    extra += f" | Op: {p_op}"
                if uuid:
                    extra += f" | UUID REP: {uuid}"
                txs.append(
                    Transaccion(
                        fecha=p_f,
                        concepto=emisor_nombre,
                        importe=p_monto,
                        descripcion=extra,
                        tipo=default_tipo
                    )
                )
        return txs

    # -------------------------------------------------------------
    # SUB-LEVEL COMPLEMENT 3: EstadoDeCuentaCombustible (ecc11 / ecc12)
    # -------------------------------------------------------------
    ecc_items = comp.xpath(".//*[local-name()='EstadoDeCuentaCombustible']//*[local-name()='ConceptoEstadoDeCuentaCombustible']")
    if ecc_items:
        for c_ecc in ecc_items:
            ecc_f = normalize_spanish_date(c_ecc.get("Fecha") or c_ecc.get("fecha") or fecha)
            ecc_imp_str = c_ecc.get("Importe") or c_ecc.get("importe") or "0"
            try:
                ecc_imp = abs(float(ecc_imp_str.replace(",", "").strip()))
            except ValueError:
                ecc_imp = 0.0

            ecc_gas = c_ecc.get("NombreGasolinera") or c_ecc.get("Rfc") or emisor_nombre
            ecc_folio = c_ecc.get("FolioOperacion") or ""
            ecc_comb = c_ecc.get("Combustible") or ""
            extra = f"Combustible: {ecc_comb} | Estación: {ecc_gas}"
            if ecc_folio:
                extra += f" | Op: {ecc_folio}"
            if uuid:
                extra += f" | UUID: {uuid}"
            txs.append(
                Transaccion(
                    fecha=ecc_f,
                    concepto=ecc_gas,
                    importe=ecc_imp,
                    descripcion=extra,
                    tipo=TipoTransaccion.EGRESO
                )
            )
        return txs

    # -------------------------------------------------------------
    # SUB-LEVEL COMPLEMENT 4: Nómina
    # -------------------------------------------------------------
    nom_elems = comp.xpath(".//*[local-name()='Nomina']")
    if nom_elems:
        nom = nom_elems[0]
        nom_f = normalize_spanish_date(nom.get("FechaPago") or nom.get("fechaPago") or fecha)
        nom_per = nom.get("TotalPercepciones") or "0"
        nom_ded = nom.get("TotalDeducciones") or "0"
        extra = f"Nómina | Percepciones: ${nom_per} | Deducciones: ${nom_ded} | Patrón: {emisor_nombre} ({emisor_rfc})"
        if uuid:
            extra += f" | UUID: {uuid}"
        nom_imp = total if total > 0 else abs(float(nom_per.replace(",", "")) - float(nom_ded.replace(",", "")))
        txs.append(
            Transaccion(
                fecha=nom_f,
                concepto=f"Nómina - {emisor_nombre}",
                importe=nom_imp,
                descripcion=extra,
                tipo=default_tipo
            )
        )
        return txs

    # -------------------------------------------------------------
    # SUB-LEVEL 5: Addenda Bancaria / Estado de Cuenta (HSBC México, etc.)
    # -------------------------------------------------------------
    addenda_movs = comp.xpath(
        ".//*[local-name()='Addenda']//*[local-name()='MovimientoDelClienteFiscal' or local-name()='MovimientosDelCliente' or local-name()='Movimiento' or local-name()='movimiento']"
    )
    if not addenda_movs:
        # Fallback if Addenda tag is omitted or structured directly
        addenda_movs = comp.xpath(
            ".//*[local-name()='MovimientoDelClienteFiscal' or local-name()='MovimientosDelCliente']"
        )

    if addenda_movs:
        datos_elems = comp.xpath(".//*[local-name()='DatosGenerales']")
        cuenta = ""
        periodo = ""
        if datos_elems:
            cuenta = datos_elems[0].get("numerodecuenta") or datos_elems[0].get("numeroDeCuenta") or ""
            periodo = datos_elems[0].get("periodo") or ""
        if not cuenta:
            cuenta = folio_completo if folio_completo != "S/F" else ""

        mov_fiscales = comp.xpath(".//*[local-name()='MovimientoDelClienteFiscal']")
        cuenta_clean = re.sub(r"\D", "", cuenta)
        is_credit_card = bool(mov_fiscales) or len(cuenta_clean) == 16 or cuenta.strip().startswith("5470")

        for m in addenda_movs:
            tag_name = m.tag.split("}")[-1] if "}" in m.tag else m.tag
            m_fecha_raw = m.get("fecha") or m.get("Fecha") or fecha
            m_fecha = normalize_spanish_date(m_fecha_raw)
            m_desc = m.get("descripcion") or m.get("Descripcion") or "Movimiento bancario"
            m_desc = " ".join(m_desc.split())
            m_imp_str = m.get("importe") or m.get("Importe") or "0"
            try:
                raw_num = float(m_imp_str.replace(",", "").strip())
                m_imp = abs(raw_num)
            except ValueError:
                raw_num = 0.0
                m_imp = 0.0

            m_tipo_attr = (m.get("tipo") or m.get("Tipo") or "").upper()

            if tag_name == "MovimientoDelClienteFiscal":
                m_flow = TipoTransaccion.EGRESO
                rfc_enaj = m.get("RFCenajenante") or m.get("rfcEnajenante") or ""
                extra = f"{m_desc} | RFC Enajenante: {rfc_enaj}" if rfc_enaj else m_desc
            elif "ABONO" in m_tipo_attr or "DEPOSITO" in m_tipo_attr:
                m_flow = TipoTransaccion.INGRESO
                extra = m_desc
            elif "CARGO" in m_tipo_attr or "RETIRO" in m_tipo_attr or raw_num < 0:
                m_flow = TipoTransaccion.EGRESO
                extra = m_desc
            else:
                d_upper = m_desc.upper()
                if EXPENSE_REGEX.search(d_upper):
                    m_flow = TipoTransaccion.EGRESO
                elif INCOME_REGEX.search(d_upper):
                    m_flow = TipoTransaccion.INGRESO
                elif is_credit_card:
                    if re.search(r"\bPAGO[S]?\b", d_upper):
                        m_flow = TipoTransaccion.INGRESO
                    else:
                        m_flow = TipoTransaccion.EGRESO
                else:
                    m_flow = default_tipo
                extra = m_desc

            if cuenta:
                extra += f" | Cuenta: {cuenta}"
            if emisor_nombre and emisor_nombre != "Emisor Desconocido":
                extra += f" | Banco: {emisor_nombre}"
            if uuid:
                extra += f" | UUID: {uuid}"

            txs.append(
                Transaccion(
                    fecha=m_fecha,
                    concepto=m_desc,
                    importe=m_imp,
                    descripcion=extra,
                    tipo=m_flow
                )
            )
        return txs

    # -------------------------------------------------------------
    # STANDARD INVOICE LINE ITEMS (Conceptos) & TAX INCLUSION
    # -------------------------------------------------------------
    conceptos = comp.xpath(".//*[local-name()='Concepto']")
    if conceptos:
        tax_ratio = (total / subtotal) if (subtotal > 0 and total > 0) else 1.0
        for c in conceptos:
            desc = c.get("Descripcion") or c.get("descripcion") or "Sin descripción"
            imp_sub_str = c.get("Importe") or c.get("importe") or c.get("ValorUnitario") or str(total)
            try:
                imp_sub = abs(float(imp_sub_str.replace(",", "").strip()))
            except ValueError:
                imp_sub = total

            # Check child taxes per concepto
            traslados = sum(abs(float((t.get("Importe") or t.get("importe") or "0").replace(",", ""))) for t in c.xpath(".//*[local-name()='Traslado']"))
            retenciones = sum(abs(float((r.get("Importe") or r.get("importe") or "0").replace(",", ""))) for r in c.xpath(".//*[local-name()='Retencion']"))
            descuento = abs(float((c.get("Descuento") or c.get("descuento") or "0").replace(",", "")))

            if traslados > 0 or retenciones > 0:
                imp_final = round(imp_sub + traslados - retenciones - descuento, 2)
            elif len(conceptos) == 1 and total > 0:
                imp_final = total
            elif subtotal > 0 and total > 0 and abs(total - subtotal) > 0.01:
                imp_final = round(imp_sub * tax_ratio, 2)
            else:
                imp_final = imp_sub

            clave = c.get("ClaveProdServ") or c.get("claveProdServ") or ""
            extra = f"{desc} | Folio: {folio_completo} | Emisor: {emisor_nombre} ({emisor_rfc})"
            if uuid:
                extra += f" | UUID: {uuid}"
            if clave:
                extra += f" | Clave: {clave}"
            if moneda.upper() not in ("MXN", "XXX") and tipo_cambio > 0:
                extra += f" | Moneda: {moneda} (TC: {tipo_cambio})"

            txs.append(
                Transaccion(
                    fecha=fecha,
                    concepto=emisor_nombre,
                    importe=imp_final,
                    descripcion=extra,
                    tipo=default_tipo
                )
            )
    else:
        extra = f"CFDI {tipo_comprobante} Folio: {folio_completo} | Emisor: {emisor_nombre} ({emisor_rfc})"
        if uuid:
            extra += f" | UUID: {uuid}"
        if moneda.upper() not in ("MXN", "XXX") and tipo_cambio > 0:
            extra += f" | Moneda: {moneda} (TC: {tipo_cambio})"
        txs.append(
            Transaccion(
                fecha=fecha,
                concepto=emisor_nombre,
                importe=total,
                descripcion=extra,
                tipo=default_tipo
            )
        )

    return txs


def _parse_ofx_xml(root: etree._Element) -> List[Transaccion]:

    """
    Parses Open Financial Exchange (OFX) XML banking statement files deterministically.
    """
    txs: List[Transaccion] = []
    stmttrns = root.xpath(".//*[translate(local-name(), 'stmttrn', 'STMTTRN')='STMTTRN']")
    for trn in stmttrns:
        dt_elem = trn.xpath(".//*[translate(local-name(), 'dtposted', 'DTPOSTED')='DTPOSTED']")
        raw_dt = (dt_elem[0].text or "").strip() if dt_elem else ""
        fecha = normalize_spanish_date(raw_dt) if raw_dt else "2024-01-01"

        amt_elem = trn.xpath(".//*[translate(local-name(), 'trnamt', 'TRNAMT')='TRNAMT']")
        raw_amt = (amt_elem[0].text or "0").strip() if amt_elem else "0"
        try:
            amt_val = float(raw_amt.replace(",", ""))
        except ValueError:
            amt_val = 0.0

        type_elem = trn.xpath(".//*[translate(local-name(), 'trntype', 'TRNTYPE')='TRNTYPE']")
        trn_type = (type_elem[0].text or "").strip().upper() if type_elem else ""

        flow = TipoTransaccion.EGRESO if (amt_val < 0 or trn_type in ["DEBIT", "CHECK", "PAYMENT", "FEE"]) else TipoTransaccion.INGRESO

        name_elem = trn.xpath(".//*[translate(local-name(), 'name', 'NAME')='NAME']")
        memo_elem = trn.xpath(".//*[translate(local-name(), 'memo', 'MEMO')='MEMO']")
        fitid_elem = trn.xpath(".//*[translate(local-name(), 'fitid', 'FITID')='FITID']")

        name = (name_elem[0].text or "").strip() if name_elem else ""
        memo = (memo_elem[0].text or "").strip() if memo_elem else ""
        fitid = (fitid_elem[0].text or "").strip() if fitid_elem else ""

        concepto = name or memo or "Transacción Bancaria"
        desc_parts = [memo or name]
        if fitid:
            desc_parts.append(f"FITID: {fitid}")
        if trn_type:
            desc_parts.append(f"Tipo: {trn_type}")

        txs.append(
            Transaccion(
                fecha=fecha,
                concepto=concepto,
                importe=abs(amt_val),
                descripcion=" | ".join(desc_parts),
                tipo=flow
            )
        )
    return txs


def _parse_camt_xml(root: etree._Element) -> List[Transaccion]:
    """
    Parses ISO 20022 CAMT (camt.052, camt.053, camt.054) XML banking statements.
    """
    txs: List[Transaccion] = []
    entries = root.xpath(".//*[local-name()='Ntry']")
    for ntry in entries:
        amt_elem = ntry.xpath(".//*[local-name()='Amt']")
        raw_amt = (amt_elem[0].text or "0").strip() if amt_elem else "0"
        try:
            amt_val = abs(float(raw_amt.replace(",", "")))
        except ValueError:
            amt_val = 0.0

        cdt_dbt = ntry.xpath(".//*[local-name()='CdtDbtInd']")
        indicator = (cdt_dbt[0].text or "DBIT").strip().upper() if cdt_dbt else "DBIT"
        flow = TipoTransaccion.INGRESO if indicator == "CRDT" else TipoTransaccion.EGRESO

        dt_elem = ntry.xpath(".//*[local-name()='BookgDt']//*[local-name()='Dt']") or ntry.xpath(".//*[local-name()='ValDt']//*[local-name()='Dt']")
        raw_dt = (dt_elem[0].text or "").strip() if dt_elem else ""
        fecha = normalize_spanish_date(raw_dt) if raw_dt else "2024-01-01"

        rmt_elem = ntry.xpath(".//*[local-name()='RmtInf']//*[local-name()='Ustrd']")
        desc = (rmt_elem[0].text or "").strip() if rmt_elem else ""

        party_elem = ntry.xpath(".//*[local-name()='RltdPties']//*[local-name()='Nm']")
        party = (party_elem[0].text or "").strip() if party_elem else ""

        concepto = party or desc or "Movimiento CAMT"
        full_desc = f"{desc or party} | Indicador: {indicator}"
        txs.append(
            Transaccion(
                fecha=fecha,
                concepto=concepto,
                importe=amt_val,
                descripcion=full_desc,
                tipo=flow
            )
        )
    return txs


def _parse_excel_xml(root: etree._Element) -> Tuple[Optional[List[Transaccion]], Optional[str]]:
    """
    Parses Excel XML Spreadsheet format (urn:schemas-microsoft-com:office:spreadsheet)
    exported by banks into structured tabular Markdown for the extraction pipeline.
    """
    rows_elems = root.xpath(".//*[local-name()='Row']")
    if not rows_elems:
        return None, None
    table_data = []
    for row in rows_elems:
        cells = row.xpath(".//*[local-name()='Cell']")
        row_vals = []
        for cell in cells:
            data_elem = cell.xpath(".//*[local-name()='Data']")
            val = (data_elem[0].text or "").strip() if data_elem else ""
            row_vals.append(val)
        if any(row_vals):
            table_data.append(row_vals)

    if not table_data:
        return None, None

    max_cols = max(len(r) for r in table_data)
    md_lines = []
    headers = table_data[0] + [""] * (max_cols - len(table_data[0]))
    header_clean = [h.replace("|", "/") or f"Col_{i+1}" for i, h in enumerate(headers)]
    md_lines.append("| " + " | ".join(header_clean) + " |")
    md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
    for r in table_data[1:]:
        padded = r + [""] * (max_cols - len(r))
        cells_clean = [c.replace("|", "/") for c in padded]
        md_lines.append("| " + " | ".join(cells_clean) + " |")

    return None, "\n".join(md_lines)


def _parse_generic_financial_xml(root: etree._Element) -> Tuple[Optional[List[Transaccion]], Optional[str]]:
    """
    Scans XML tree for repeating child elements representing individual financial movements.
    Attempts deterministic parsing if standard cues are found, or converts to a structured
    Markdown table preserving all child levels.
    """
    candidate_tags = [
        "movimiento", "transaccion", "operacion", "registro", "row", "item",
        "entry", "record", "transaction", "pago", "cargo", "abono",
        "movement", "detail", "detalle", "linea", "partida"
    ]

    best_nodes = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1].lower() if "}" in elem.tag else elem.tag.lower()
        if tag in candidate_tags:
            best_nodes.append(elem)

    if len(best_nodes) < 2:
        tag_counts: Dict[str, int] = {}
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

        for tag, count in tag_counts.items():
            if count >= 2:
                sample_nodes = root.xpath(f".//*[local-name()='{tag}']")
                has_financial = False
                for node in sample_nodes[:3]:
                    node_text = " ".join([node.text or ""] + [v for v in node.attrib.values()] + [(child.text or "") for child in node.iter()])
                    if DATE_PATTERN.search(node_text) and MONEY_PATTERN.search(node_text):
                        has_financial = True
                        break
                if has_financial:
                    best_nodes = sample_nodes
                    break

    if len(best_nodes) >= 2:
        table_rows = []
        for node in best_nodes:
            fields = {}
            for k, v in node.attrib.items():
                if v:
                    fields[k] = v
            for child in node:
                c_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                c_text = (child.text or "").strip()
                if c_text:
                    fields[c_tag] = c_text
                for ck, cv in child.attrib.items():
                    if cv:
                        fields[f"{c_tag}_{ck}"] = cv
            if fields:
                table_rows.append(fields)

        if table_rows:
            txs = []
            deterministic_success = True
            for r in table_rows:
                f_val = ""
                for k, v in r.items():
                    k_low = k.lower()
                    if ("fec" in k_low or "date" in k_low or "dia" in k_low) and DATE_PATTERN.search(v):
                        f_val = normalize_spanish_date(v)
                        break

                cargo_val = 0.0
                abono_val = 0.0
                imp_val = 0.0
                found_cargo_abono = False

                for k, v in r.items():
                    k_low = k.lower()
                    if "cargo" in k_low or "retiro" in k_low or "debit" in k_low:
                        try:
                            clean_v = v.replace("$", "").replace(",", "").strip()
                            val = abs(float(clean_v))
                            if val > 0:
                                cargo_val = val
                                found_cargo_abono = True
                        except ValueError:
                            pass
                    elif "abono" in k_low or "deposito" in k_low or "credit" in k_low:
                        try:
                            clean_v = v.replace("$", "").replace(",", "").strip()
                            val = abs(float(clean_v))
                            if val > 0:
                                abono_val = val
                                found_cargo_abono = True
                        except ValueError:
                            pass
                    elif ("imp" in k_low or "monto" in k_low or "amount" in k_low or "total" in k_low) and not found_cargo_abono:
                        try:
                            clean_v = v.replace("$", "").replace(",", "").strip()
                            imp_val = abs(float(clean_v))
                        except ValueError:
                            pass

                concept_val = ""
                for k, v in r.items():
                    k_low = k.lower()
                    if any(c in k_low for c in ["concep", "desc", "detall", "nom", "name", "memo", "bene", "comer"]):
                        concept_val = v
                        break
                if not concept_val:
                    concept_val = "Transacción XML"

                if f_val and (found_cargo_abono or imp_val > 0):
                    if found_cargo_abono:
                        if cargo_val > 0:
                            tx_monto = cargo_val
                            tx_tipo = TipoTransaccion.EGRESO
                        else:
                            tx_monto = abono_val
                            tx_tipo = TipoTransaccion.INGRESO
                    else:
                        tx_monto = imp_val
                        type_str = ""
                        for k, v in r.items():
                            if "tip" in k.lower() or "type" in k.lower():
                                type_str = v.upper()
                                break
                        if "ABONO" in type_str or "DEP" in type_str or "ING" in type_str or "CRDT" in type_str:
                            tx_tipo = TipoTransaccion.INGRESO
                        else:
                            tx_tipo = TipoTransaccion.EGRESO

                    desc_all = " | ".join(f"{k}: {v}" for k, v in r.items() if len(v) < 80)
                    txs.append(
                        Transaccion(
                            fecha=f_val,
                            concepto=concept_val,
                            importe=tx_monto,
                            descripcion=desc_all,
                            tipo=tx_tipo
                        )
                    )
                else:
                    deterministic_success = False
                    break

            if deterministic_success and len(txs) == len(table_rows):
                return txs, None

            # Generate markdown table preserving all columns
            all_cols = []
            for r in table_rows:
                for k in r.keys():
                    if k not in all_cols:
                        all_cols.append(k)

            md_lines = []
            md_lines.append("| " + " | ".join(all_cols) + " |")
            md_lines.append("| " + " | ".join(["---"] * len(all_cols)) + " |")
            for r in table_rows:
                row_vals = [r.get(c, "").replace("|", "/") for c in all_cols]
                md_lines.append("| " + " | ".join(row_vals) + " |")

            return None, "\n".join(md_lines)

    return None, None


def _extract_hierarchical_xml_text(root: etree._Element) -> str:
    """
    Recursively extracts tag names, attributes, and text content with clean indentation
    preserving parent-child hierarchy across all nested levels for LLM analysis.
    """
    lines = []
    def _traverse(elem: etree._Element, depth: int = 0):
        indent = "  " * depth
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        text = (elem.text or "").strip()
        attrs = " ".join([f"{k}='{v}'" for k, v in elem.attrib.items() if v])
        parts = [f"{indent}{tag}"]
        if attrs:
            parts.append(f"[{attrs}]")
        if text:
            parts.append(f": {text}")
        if len(parts) > 1 or elem.attrib:
            lines.append(" ".join(parts))
        for child in elem:
            _traverse(child, depth + 1)

    _traverse(root)
    return "\n".join(lines)


def resolve_pdf_strategy(
    strat_choice: str,
    general_strategy: str = "🎯 Máxima Precisión (Forzar RapidOCR + Accurate Tables + Escala 2.5x)",
    custom_ocr: Optional[bool] = None,
    custom_accurate: bool = True,
    custom_scale: float = 2.5
) -> dict:
    """
    Resolves the effective parameters for PDF extraction based on general defaults
    and optional per-file overrides.
    Default across the entire system is '🎯 Máxima Precisión' (RapidOCR + Accurate Tables + 2.5x).
    """
    effective = general_strategy if strat_choice.startswith("🌐 Usar Estrategia General") else strat_choice

    if "Máxima Precisión" in effective:
        return {
            "strategy_label": "🎯 Máxima Precisión",
            "enable_ocr": True,
            "accurate_tables": True,
            "ocr_scale": 2.5
        }
    elif "Digital Nativo" in effective:
        return {
            "strategy_label": "⚡ Digital Nativo",
            "enable_ocr": False,
            "accurate_tables": False,
            "ocr_scale": 2.0
        }
    elif "Escaneo Físico" in effective:
        return {
            "strategy_label": "🔍 Escaneo Físico",
            "enable_ocr": True,
            "accurate_tables": False,
            "ocr_scale": 2.0
        }
    elif "Personalizado" in effective:
        return {
            "strategy_label": "🛠️ Personalizado",
            "enable_ocr": custom_ocr,
            "accurate_tables": custom_accurate,
            "ocr_scale": custom_scale
        }
    else:  # Automático
        return {
            "strategy_label": "🤖 Automático",
            "enable_ocr": None,
            "accurate_tables": False,
            "ocr_scale": 2.0
        }


def resolve_xml_strategy(
    strat_choice: str,
    general_strategy: str = "🎯 Máxima Precisión (Determinista Inteligente + Fallback Híbrido LLM)"
) -> dict:
    """
    Resolves the effective parameters for XML extraction based on general defaults
    and optional per-file overrides.
    Default across the entire system is '🎯 Máxima Precisión' (Deterministic + Hybrid LLM fallback).
    """
    effective = general_strategy if strat_choice.startswith("🌐 Usar Estrategia General") else strat_choice

    if "Máxima Precisión" in effective or "Híbrido" in effective:
        return {
            "strategy_label": "🎯 Máxima Precisión",
            "xml_mode": "hybrid"
        }
    elif "Determinista" in effective:
        return {
            "strategy_label": "⚡ Determinista Estricto",
            "xml_mode": "deterministic"
        }
    else:  # Forzar LLM
        return {
            "strategy_label": "🤖 Forzar Ollama LLM",
            "xml_mode": "llm"
        }


_BAL_AMT_REGEX = r"(?:\(\s*[-−–]?\s*\$?\s*[\d,]+\.\d{2}\s*(?:MXN|USD)?\s*\)|[-−–]?\s*\$?\s*[\d,]+\.\d{2}\s*[-−–]?(?:\s*(?:CR|DR|DB|MXN|USD))?)"


def _extract_balance_amount(label_pattern: str, text: str) -> Optional[float]:
    """
    Matches label_pattern followed optionally by multiline separator and monetary amount.
    Supports negative formats: -1,500.00, (1,500.00), 1,500.00-, 1,500.00 DR/DB/CR.
    """
    pattern = (
        r"(?i)(?:" + label_pattern + r")"
        r"(?:[:\s\t\|]|\*\*|-(?![\$\d\s]))*(?:\r?\n)?[\s\t\|:]*"
        r"(" + _BAL_AMT_REGEX + r")"
    )
    m = re.search(pattern, text)
    if m:
        raw_val = m.group(1).strip()
        return sanitize_importe(raw_val, keep_sign=True)
    return None


def extract_statement_balances(text: str) -> Dict[str, Optional[float]]:
    """
    Deterministically extracts opening (saldo_inicial) and closing (saldo_final)
    balances from document text, markdown tables, or XML representations.

    Supports:
    - Overdraft negative balances: -1,500.00, (1,500.00), 1,500.00-, DR, DB, CR.
    - Multiline extraction where amount appears on subsequent line (Saldo Inicial\n$1,500.00).
    - Credit card statement patterns: 'Saldo deudor total', 'Saldo anterior', 'Nuevo saldo',
      'Total cargos', 'Total abonos'.

    Args:
        text: Raw document text, markdown content, or XML string.

    Returns:
        Dict[str, Optional[float]]: Dictionary containing extracted balances and summary totals.
    """
    balances: Dict[str, Optional[float]] = {
        "saldo_inicial": None,
        "saldo_final": None,
        "saldo_anterior": None,
        "saldo_deudor_total": None,
        "nuevo_saldo": None,
        "total_cargos": None,
        "total_abonos": None,
    }
    if not text or not isinstance(text, str):
        return balances

    # 1. XML attribute checks (e.g. ecb:EstadoDeCuentaBancario or OFX LEDGERBAL)
    ecb_ini = re.search(r"""(?:saldoInicial|saldoAnterior)\s*=\s*["']\s*([-\d,]+\.\d{2}[^"']*)["']""", text, re.I)
    if ecb_ini:
        try:
            balances["saldo_inicial"] = sanitize_importe(ecb_ini.group(1), keep_sign=True)
            balances["saldo_anterior"] = balances["saldo_inicial"]
        except Exception:
            pass

    ecb_fin = re.search(r"""(?:saldoFinal|saldoAlCorte)\s*=\s*["']\s*([-\d,]+\.\d{2}[^"']*)["']""", text, re.I)
    if ecb_fin:
        try:
            balances["saldo_final"] = sanitize_importe(ecb_fin.group(1), keep_sign=True)
        except Exception:
            pass

    ofx_bal = re.search(r"""<LEDGERBAL>[\s\S]*?<BALAMT>\s*([-\d,]+\.\d{2})""", text, re.I)
    if ofx_bal and balances["saldo_final"] is None:
        try:
            balances["saldo_final"] = sanitize_importe(ofx_bal.group(1), keep_sign=True)
        except Exception:
            pass

    # 2. Extract specific credit card and banking labels (single or multi-line)
    saldo_anterior = _extract_balance_amount(r"saldo\s+(?:deudor\s+)?anterior(?:\s+al\s+corte)?", text)
    saldo_inicial = _extract_balance_amount(r"saldo\s+inicial(?:\s+del\s+periodo)?|saldo\s+al\s+inicio", text)
    saldo_actual = _extract_balance_amount(r"saldo\s+actual(?:\s+al\s+corte)?|saldo\s+final(?:\s+al\s+corte)?|saldo\s+al\s+corte", text)
    nuevo_saldo = _extract_balance_amount(r"nuevo\s+saldo(?:\s+al\s+corte)?", text)
    saldo_deudor_total = _extract_balance_amount(r"saldo\s+deudor\s+total|saldo\s+deudor(?:\s+total)?", text)
    total_cargos = _extract_balance_amount(
        r"total\s+(?:de\s+)?cargos?|total\s+(?:de\s+)?retiros?(?:\s*/\s*cargos)?|compras\s+y\s+disposiciones", text
    )
    total_abonos = _extract_balance_amount(
        r"total\s+(?:de\s+)?abonos?|total\s+(?:de\s+)?dep[oó]sitos?(?:\s*/\s*abonos)?|pagos\s+y\s+cr[eé]ditos", text
    )

    balances["saldo_anterior"] = saldo_anterior
    balances["saldo_deudor_total"] = saldo_deudor_total
    balances["nuevo_saldo"] = nuevo_saldo
    balances["total_cargos"] = total_cargos
    balances["total_abonos"] = total_abonos

    # Determine primary saldo_inicial
    if balances["saldo_inicial"] is None:
        balances["saldo_inicial"] = saldo_inicial if saldo_inicial is not None else saldo_anterior

    # Determine primary saldo_final
    if balances["saldo_final"] is None:
        if saldo_actual is not None:
            balances["saldo_final"] = saldo_actual
        elif nuevo_saldo is not None:
            balances["saldo_final"] = nuevo_saldo
        elif saldo_deudor_total is not None:
            balances["saldo_final"] = saldo_deudor_total

    return balances


def infer_statement_mode(
    text: str,
    summary_data: Optional[Dict[str, Any]] = None
) -> str:
    """
    Infiere determinísticamente el modo contable del estado de cuenta:
    'debito', 'credito', o 'ambiguo'.

    Señales explícitas de Crédito:
    - límite de crédito / crédito disponible / línea de crédito
    - pago para no generar intereses / pago mínimo
    - saldo deudor / saldo deudor total / nuevo saldo (tarjeta)
    - tarjeta de crédito / pago tarjeta / compras y disposiciones

    Señales explícitas de Débito:
    - cuenta de cheques / cuenta de débito
    - clabe / clabe interbancaria
    - saldo promedio / saldo disponible (bancario)
    - depósito nómina / retiro cajero

    Cruce matemático contra summary_data (totales declarados):
    - Verifica si saldo_inicial + total_abonos - total_cargos == saldo_final (débito)
    - o saldo_inicial - total_abonos + total_cargos == saldo_final (crédito)
    """
    raw_text = (text or "").lower()
    sum_data = summary_data or {}

    # 1. Indicadores textuales de crédito
    credit_indicators = [
        "límite de crédito", "limite de credito", "crédito disponible", "credito disponible",
        "línea de crédito", "linea de credito",
        "pago para no generar intereses", "pago minimo", "pago mínimo",
        "saldo deudor total", "saldo deudor", "tarjeta de crédito", "tarjeta de credito",
        "tarjeta", "pago tarjeta", "compras y disposiciones"
    ]
    credit_score = sum(1 for ind in credit_indicators if ind in raw_text)

    # 2. Indicadores textuales de débito
    debit_indicators = [
        "cuenta de cheques", "cuenta cheques", "cuenta de débito", "cuenta de debito",
        "clabe", "clabe interbancaria", "saldo promedio", "saldo disponible",
        "depósito nómina", "deposito nomina", "retiro en cajero", "retiro cajero", "cajero automático"
    ]
    debit_score = sum(1 for ind in debit_indicators if ind in raw_text)

    # 3. Cruce matemático contra totales declarados en el resumen del estado de cuenta
    s_ini = sum_data.get("saldo_inicial") if sum_data.get("saldo_inicial") is not None else sum_data.get("saldo_anterior")
    s_fin = sum_data.get("saldo_final") if sum_data.get("saldo_final") is not None else (
        sum_data.get("nuevo_saldo") or sum_data.get("saldo_deudor_total") or sum_data.get("saldo_actual")
    )
    t_cargos = sum_data.get("total_cargos")
    t_abonos = sum_data.get("total_abonos")

    math_mode = None
    if s_ini is not None and s_fin is not None and t_cargos is not None and t_abonos is not None:
        deb_calc = round(float(s_ini) + float(t_abonos) - float(t_cargos), 2)
        cred_calc = round(float(s_ini) - float(t_abonos) + float(t_cargos), 2)
        target = round(float(s_fin), 2)

        match_deb = abs(deb_calc - target) <= 0.05
        match_cred = abs(cred_calc - target) <= 0.05

        if match_deb and not match_cred:
            math_mode = "debito"
        elif match_cred and not match_deb:
            math_mode = "credito"

    # Resolución combinada
    if math_mode == "debito" and credit_score == 0:
        return "debito"
    if math_mode == "credito" and debit_score == 0:
        return "credito"
    if math_mode and math_mode == ("credito" if credit_score > debit_score else "debito"):
        return math_mode

    if credit_score > 0 and debit_score == 0:
        return "credito"
    if debit_score > 0 and credit_score == 0:
        return "debito"

    if math_mode:
        return math_mode

    return "ambiguo"


def _clean_cell(cell: str) -> str:
    """Cleans cell markdown formatting, bold/italics markers, and leading/trailing whitespace."""
    return cell.replace("**", "").replace("__", "").replace("\\|", "|").strip()


def _identify_table_columns(headers: List[str]) -> Dict[str, Optional[int]]:
    """
    Normaliza encabezados mediante expresiones regulares para mapear los índices de columna:
    - col_fecha: fecha, f. oper, f. liq, dia, fechas
    - col_descripcion: concepto, descripcion, detalle, movimiento, leyenda
    - col_cargo: cargo, cargos, retiro, retiros, disposicion, debito, salidas, egresos
    - col_abono: abono, abonos, deposito, depositos, pago, pagos, credito, entradas, ingresos
    - col_importe_unico: importe, monto, valor, cantidad
    - col_saldo: saldo, balance, saldo disponible, saldo actual
    - col_referencia: referencia, folio, autorizacion, rastreo, aut, ref
    """
    mapping: Dict[str, Optional[int]] = {
        "fecha": None,
        "descripcion": None,
        "cargo": None,
        "abono": None,
        "importe_unico": None,
        "saldo": None,
        "referencia": None,
    }
    for idx, raw_h in enumerate(headers):
        h = _clean_cell(raw_h).lower()
        if not h:
            continue
        # Check saldo first to avoid 'saldo deudor' matching cargo
        if re.search(r"\b(?:saldo|balance)\b", h):
            if mapping["saldo"] is None:
                mapping["saldo"] = idx
        elif re.search(r"\b(?:cargos?|retiros?|disposici[oó]n(?:es)?|d[eé]bitos?|salidas?|egresos?|compras?)\b", h):
            if mapping["cargo"] is None:
                mapping["cargo"] = idx
        elif re.search(r"\b(?:abonos?|dep[oó]sitos?|pagos?|cr[eé]ditos?|entradas?|ingresos?)\b", h):
            if mapping["abono"] is None:
                mapping["abono"] = idx
        elif re.search(r"\b(?:fechas?|f\.?\s*oper(?:aci[oó]n)?|f\.?\s*liq(?:uidaci[oó]n)?|d[ií]a|date)\b", h):
            if mapping["fecha"] is None:
                mapping["fecha"] = idx
        elif re.search(r"\b(?:referencia|folio|autorizaci[oó]n|aut|rastreo|ref)\b", h):
            if mapping["referencia"] is None:
                mapping["referencia"] = idx
        elif re.search(r"\b(?:concepto|descripci[oó]n|detalle|movimiento|leyenda|comercio|establecimiento|transacci[oó]n)\b", h):
            if mapping["descripcion"] is None:
                mapping["descripcion"] = idx
        elif re.search(r"\b(?:importe|monto|valor|cantidad|amount)\b", h):
            if mapping["importe_unico"] is None:
                mapping["importe_unico"] = idx

    # If both cargo and abono exist, single-amount column is not used
    if mapping["cargo"] is not None and mapping["abono"] is not None:
        mapping["importe_unico"] = None

    return mapping


def parse_docling_markdown_tables(
    markdown_text: str,
    modo_contable: str = "debito",
    statement_period: Optional[Tuple[str, str]] = None,
    default_page: int = 1
) -> Tuple[List[Transaccion], List[str], Dict[str, Any]]:
    """
    Deterministic Markdown Table Parser for Docling outputs.
    
    Identifies Markdown table blocks, maps column indices, merges multiline
    descriptions, determines transaction cashflow types, and detects shifted cells.
    
    Returns:
        (transactions: List[Transaccion], unparsed_candidate_lines: List[str], meta: Dict[str, Any])
    """
    if not markdown_text or not isinstance(markdown_text, str):
        return [], [], {"total_candidate_lines": 0, "captured_candidate_lines": 0, "unparsed_candidate_lines_count": 0, "table_transactions_count": 0, "tables_detected": 0}

    statement_year = extract_statement_year(markdown_text)
    effective_period = statement_period or extract_statement_period(markdown_text)
    all_transactions: List[Transaccion] = []
    captured_candidate_lines: set = set()

    # Split into contiguous blocks of table lines and track page numbers
    lines = markdown_text.splitlines()
    table_blocks: List[Tuple[int, List[str]]] = []
    current_block: List[str] = []
    current_page = default_page

    for line in lines:
        s_line = line.strip()
        p_match = re.search(r"(?:<!--\s*page(?:_no)?:\s*(\d+)\s*-->|<!--\s*page\s+(\d+)\s*-->|^#+\s*P[aá]gina\s+(\d+)|<!--\s*Page\s+(\d+)\s*-->)", s_line, re.I)
        if p_match:
            current_page = int(next(g for g in p_match.groups() if g is not None))

        if "|" in s_line and s_line.startswith("|") and s_line.endswith("|"):
            current_block.append(s_line)
        else:
            if current_block:
                table_blocks.append((current_page, current_block))
                current_block = []
    if current_block:
        table_blocks.append((current_page, current_block))

    for block_page, block in table_blocks:
        if len(block) < 3:
            continue

        # Find separator line
        sep_idx = None
        for i, l in enumerate(block):
            if re.search(r"^\|\s*(?::?-{2,}:?\s*\|)+\s*$", l):
                sep_idx = i
                break

        if sep_idx is None or sep_idx == 0:
            continue

        # Inspect headers: pick the row right before separator
        header_row = block[sep_idx - 1]
        raw_headers = [c for c in header_row.split("|")]
        if len(raw_headers) >= 2 and raw_headers[0] == "":
            raw_headers = raw_headers[1:]
        if len(raw_headers) >= 1 and raw_headers[-1] == "":
            raw_headers = raw_headers[:-1]

        cols = _identify_table_columns(raw_headers)

        # Check if table has a date column and at least one financial amount column
        has_amt_col = (cols["cargo"] is not None) or (cols["abono"] is not None) or (cols["importe_unico"] is not None)
        if cols["fecha"] is None or not has_amt_col:
            continue

        table_txs: List[Transaccion] = []
        data_rows = block[sep_idx + 1 :]

        for data_line in data_rows:
            raw_cells = [c for c in data_line.split("|")]
            if len(raw_cells) >= 2 and raw_cells[0] == "":
                raw_cells = raw_cells[1:]
            if len(raw_cells) >= 1 and raw_cells[-1] == "":
                raw_cells = raw_cells[:-1]

            if not raw_cells or all(not _clean_cell(c) for c in raw_cells):
                continue

            cleaned_cells = [_clean_cell(c) for c in raw_cells]

            col_fecha = cols["fecha"]
            col_desc = cols["descripcion"]
            col_cargo = cols["cargo"]
            col_abono = cols["abono"]
            col_imp = cols["importe_unico"]
            col_saldo = cols["saldo"]
            col_ref = cols["referencia"]

            fecha_raw = cleaned_cells[col_fecha] if col_fecha < len(cleaned_cells) else ""
            cargo_raw = cleaned_cells[col_cargo] if col_cargo is not None and col_cargo < len(cleaned_cells) else ""
            abono_raw = cleaned_cells[col_abono] if col_abono is not None and col_abono < len(cleaned_cells) else ""
            imp_raw = cleaned_cells[col_imp] if col_imp is not None and col_imp < len(cleaned_cells) else ""
            saldo_raw = cleaned_cells[col_saldo] if col_saldo is not None and col_saldo < len(cleaned_cells) else ""

            has_date = bool(DATE_PATTERN.search(fecha_raw))
            has_cargo = bool(MONEY_PATTERN.search(cargo_raw))
            has_abono = bool(MONEY_PATTERN.search(abono_raw))
            has_imp = bool(MONEY_PATTERN.search(imp_raw))
            has_any_amt = has_cargo or has_abono or has_imp

            # -------------------------------------------------------------
            # FUSIÓN DE DESCRIPCIONES MULTILÍNEA
            # -------------------------------------------------------------
            if not has_date and not has_any_amt:
                desc_text = cleaned_cells[col_desc] if col_desc is not None and col_desc < len(cleaned_cells) else ""
                if not desc_text:
                    desc_text = " ".join(c for c in cleaned_cells if c)
                target_tx = table_txs[-1] if table_txs else (all_transactions[-1] if all_transactions else None)
                if desc_text and target_tx:
                    target_tx.descripcion = f"{target_tx.descripcion} {desc_text}".strip()
                    target_tx.linea_origen = f"{target_tx.linea_origen}\n{data_line}".strip()
                    captured_candidate_lines.add(data_line.strip())
                continue

            # -------------------------------------------------------------
            # DETERMINACIÓN DE VALORES DE LA FILA
            # -------------------------------------------------------------
            if has_date:
                norm_date = normalize_spanish_date(fecha_raw, default_year=statement_year, statement_period=effective_period)
            else:
                norm_date = table_txs[-1].fecha if table_txs else "2024-01-01"

            desc_cell = cleaned_cells[col_desc] if col_desc is not None and col_desc < len(cleaned_cells) else ""
            concepto = desc_cell.strip() or "Movimiento bancario"
            descripcion = concepto

            if col_ref is not None and col_ref < len(cleaned_cells):
                ref_val = cleaned_cells[col_ref].strip()
                if ref_val:
                    descripcion += f" | Ref: {ref_val}"

            saldo_renglon = sanitize_importe(saldo_raw, keep_sign=True) if bool(MONEY_PATTERN.search(saldo_raw)) else None
            requiere_verificacion_llm = False
            importe = 0.0
            tipo = TipoTransaccion.EGRESO

            # Separated Cargo and/or Abono columns
            if col_cargo is not None or col_abono is not None:
                c_val = sanitize_importe(cargo_raw) if (col_cargo is not None and has_cargo) else 0.0
                a_val = sanitize_importe(abono_raw) if (col_abono is not None and has_abono) else 0.0

                if c_val > 0.0 and a_val == 0.0:
                    tipo = TipoTransaccion.EGRESO
                    importe = c_val
                elif a_val > 0.0 and c_val == 0.0:
                    tipo = TipoTransaccion.INGRESO
                    importe = a_val
                elif c_val > 0.0 and a_val > 0.0:
                    # Contradiction: both columns populated in same row
                    requiere_verificacion_llm = True
                    importe = max(c_val, a_val)
                    tipo = TipoTransaccion.EGRESO
                elif c_val == 0.0 and a_val == 0.0 and saldo_renglon is not None and saldo_renglon > 0.0:
                    # Cell shift in Docling: amount landed in saldo column
                    requiere_verificacion_llm = True
                    importe = saldo_renglon
                    saldo_renglon = None
                    tipo = TipoTransaccion.EGRESO
                else:
                    if not has_any_amt:
                        continue
                    importe = 0.0

            # Single amount column
            elif col_imp is not None:
                if not has_imp:
                    continue
                importe = sanitize_importe(imp_raw)
                raw_s = imp_raw.strip()
                is_neg = raw_s.startswith("-") or "(" in raw_s or raw_s.endswith("-") or "dr" in raw_s.lower() or "db" in raw_s.lower()
                prev_s = table_txs[-1].saldo_renglon if table_txs and table_txs[-1].saldo_renglon is not None else None

                if is_neg:
                    tipo = TipoTransaccion.EGRESO if modo_contable == "debito" else TipoTransaccion.INGRESO
                elif prev_s is not None and saldo_renglon is not None:
                    diff_s = round(saldo_renglon - prev_s, 2)
                    if modo_contable == "debito":
                        tipo = TipoTransaccion.INGRESO if diff_s > 0 else TipoTransaccion.EGRESO
                    else:
                        tipo = TipoTransaccion.INGRESO if diff_s < 0 else TipoTransaccion.EGRESO
                else:
                    if EXPENSE_REGEX.search(concepto.upper()):
                        tipo = TipoTransaccion.EGRESO
                    elif INCOME_REGEX.search(concepto.upper()):
                        tipo = TipoTransaccion.INGRESO
                    else:
                        tipo = TipoTransaccion.EGRESO

            if importe <= 0.0:
                continue

            tx = Transaccion(
                fecha=norm_date,
                concepto=concepto,
                importe=importe,
                descripcion=descripcion,
                tipo=tipo,
                confianza=0.50 if requiere_verificacion_llm else 0.85,
                origen="determinista_tabla",
                linea_origen=data_line,
                saldo_renglon=saldo_renglon,
                estado_revision="REVISAR" if requiere_verificacion_llm else "VERIFICADO",
                requiere_verificacion_llm=requiere_verificacion_llm,
                pagina=block_page
            )
            table_txs.append(tx)
            captured_candidate_lines.add(data_line.strip())

        all_transactions.extend(table_txs)

    # Calculate unparsed candidate lines outside detected tables
    unparsed_lines = [
        line for line in markdown_text.splitlines()
        if is_candidate_row(line) and line.strip() not in captured_candidate_lines
    ]

    total_candidates = sum(1 for line in markdown_text.splitlines() if is_candidate_row(line))
    meta = {
        "total_candidate_lines": total_candidates,
        "captured_candidate_lines": len(captured_candidate_lines),
        "unparsed_candidate_lines_count": len(unparsed_lines),
        "table_transactions_count": len(all_transactions),
        "tables_detected": len(table_blocks),
    }

    return all_transactions, unparsed_lines, meta


def validate_running_balance_chain(
    transacciones: List[Transaccion],
    saldo_inicial: Optional[float] = None,
    modo_contable: str = "debito",
    tolerance: float = 0.01,
    statement_period: Optional[Tuple[str, str]] = None
) -> Tuple[List[Transaccion], Dict[str, Any]]:
    """
    Valida aritméticamente la cadena secuencial de saldo renglón por renglón.
    Detecta celdas desplazadas (donde un cargo fue asignado a abono o viceversa)
    y ajusta automáticamente el tipo si la polaridad opuesta hace cuadrar exactamente el saldo.
    Calcula el puntaje de confianza por fila mediante compute_row_confidence.

    Returns:
        Tuple[List[Transaccion], Dict[str, Any]]: Transacciones con confianza/auditoría y métricas.
    """
    if not transacciones:
        return [], {
            "filas_con_saldo": 0,
            "filas_cumplen_cadena": 0,
            "cumplimiento_cadena_saldo": None,
            "filas_corregidas": 0,
        }

    current_balance = saldo_inicial
    if current_balance is None:
        for first_tx in transacciones:
            if first_tx.saldo_renglon is not None:
                is_ing = first_tx.tipo == TipoTransaccion.INGRESO
                if modo_contable == "debito":
                    current_balance = round(
                        first_tx.saldo_renglon - first_tx.importe if is_ing else first_tx.saldo_renglon + first_tx.importe,
                        2
                    )
                else:
                    current_balance = round(
                        first_tx.saldo_renglon + first_tx.importe if is_ing else first_tx.saldo_renglon - first_tx.importe,
                        2
                    )
                break

    filas_con_saldo = 0
    filas_cumplen = 0
    filas_corregidas = 0

    for tx in transacciones:
        row_saldo = tx.saldo_renglon
        imp = tx.importe
        is_ingreso = tx.tipo == TipoTransaccion.INGRESO

        cadena_ok: Optional[bool] = None

        was_shifted = False
        if row_saldo is not None and current_balance is not None:
            filas_con_saldo += 1
            if modo_contable == "debito":
                expected = round(current_balance + imp if is_ingreso else current_balance - imp, 2)
            else:
                expected = round(current_balance - imp if is_ingreso else current_balance + imp, 2)

            if abs(round(expected - row_saldo, 2)) <= tolerance:
                cadena_ok = True
                filas_cumplen += 1
                current_balance = row_saldo
            else:
                # Test opposite polarity (Shifted cell between cargo and abono)
                if modo_contable == "debito":
                    opp_expected = round(current_balance - imp if is_ingreso else current_balance + imp, 2)
                else:
                    opp_expected = round(current_balance + imp if is_ingreso else current_balance - imp, 2)

                if abs(round(opp_expected - row_saldo, 2)) <= tolerance:
                    # Cell shifted! Correct tipo
                    tx.tipo = TipoTransaccion.EGRESO if is_ingreso else TipoTransaccion.INGRESO
                    tx.descripcion = f"{tx.descripcion} [Ajuste: tipo invertido por saldo renglón]".strip()
                    cadena_ok = True
                    filas_cumplen += 1
                    filas_corregidas += 1
                    current_balance = row_saldo
                    was_shifted = True
                else:
                    # Broken running balance chain
                    cadena_ok = False
                    tx.estado_revision = "REVISAR"
                    current_balance = row_saldo
        elif current_balance is not None:
            # Row without saldo_renglon; progress balance according to transaction amount
            if modo_contable == "debito":
                current_balance = round(current_balance + imp if is_ingreso else current_balance - imp, 2)
            else:
                current_balance = round(current_balance - imp if is_ingreso else current_balance + imp, 2)

        # Check signal agreement
        acuerdo_senales = True
        upper_text = f"{tx.concepto} {tx.descripcion}".upper()
        if tx.tipo == TipoTransaccion.INGRESO and EXPENSE_REGEX.search(upper_text) and not INCOME_REGEX.search(upper_text):
            acuerdo_senales = False
        elif tx.tipo == TipoTransaccion.EGRESO and INCOME_REGEX.search(upper_text) and not EXPENSE_REGEX.search(upper_text):
            acuerdo_senales = False

        # Check statement period
        fecha_en_periodo = True
        if statement_period and len(statement_period) == 2:
            p_start, p_end = statement_period
            if p_start and p_end and (tx.fecha < p_start or tx.fecha > p_end):
                fecha_en_periodo = False

        # Compute confidence score
        compute_row_confidence(
            tx,
            cadena_ok=cadena_ok,
            acuerdo_senales=acuerdo_senales,
            fecha_en_periodo=fecha_en_periodo
        )

        if was_shifted:
            tx.estado_revision = "REVISAR"

    cumplimiento = (
        round((filas_cumplen / filas_con_saldo) * 100, 2)
        if filas_con_saldo > 0
        else None
    )

    metrics = {
        "filas_con_saldo": filas_con_saldo,
        "filas_cumplen_cadena": filas_cumplen,
        "cumplimiento_cadena_saldo": cumplimiento,
        "filas_corregidas": filas_corregidas
    }
    return transacciones, metrics


def identify_low_confidence_pages(
    transacciones: List[Transaccion],
    filas_rechazadas: Optional[List[Dict[str, Any]]] = None,
    cobertura: float = 1.0,
    modo_contable: str = "debito",
    tolerance: float = 0.01
) -> List[int]:
    """
    Identifica las páginas candidatas a re-extracción selectiva en alta resolución.
    Devuelve la lista ordenada y sin duplicados de páginas que cumplan cualquiera de estas condiciones:
      - Contienen al menos una fila con confianza < 0.75 o estado_revision == 'REVISAR' / 'DISCREPANCIA_OCR'.
      - Presentan una ruptura en la cadena de saldo (saldo_prev +- importe != saldo_renglon).
      - Tienen entradas asociadas en filas_rechazadas o una cobertura de página < 0.99.
    """
    if not transacciones and not filas_rechazadas:
        return []

    low_conf_pages: set[int] = set()

    # 1. Row confidence, review status, and running balance chain rupture
    current_balance: Optional[float] = None
    for tx in transacciones:
        p = getattr(tx, "pagina", 1)
        conf = getattr(tx, "confianza", 1.0)
        st = getattr(tx, "estado_revision", "VERIFICADO")

        if conf < 0.75 or st in ("REVISAR", "DISCREPANCIA_OCR"):
            low_conf_pages.add(p)

        # Check balance chain continuity
        row_saldo = getattr(tx, "saldo_renglon", None)
        if row_saldo is not None:
            if current_balance is not None:
                is_ingreso = tx.tipo == TipoTransaccion.INGRESO or str(tx.tipo).lower() in ("ingreso", "abono")
                if modo_contable == "debito":
                    expected = round(current_balance + tx.importe if is_ingreso else current_balance - tx.importe, 2)
                else:
                    expected = round(current_balance - tx.importe if is_ingreso else current_balance + tx.importe, 2)

                if abs(round(expected - row_saldo, 2)) > tolerance:
                    low_conf_pages.add(p)
            current_balance = row_saldo

    # 2. Inspect rejected rows associated with pages
    if filas_rechazadas:
        for r in filas_rechazadas:
            if isinstance(r, dict) and "pagina" in r and r["pagina"] is not None:
                try:
                    low_conf_pages.add(int(r["pagina"]))
                except Exception:
                    pass

    # 3. Overall coverage < 0.99 fallback
    if cobertura < 0.99:
        for tx in transacciones:
            low_conf_pages.add(getattr(tx, "pagina", 1))

    return sorted(list(low_conf_pages))


def reextract_pages_high_res(
    pdf_path: str,
    page_numbers: List[int]
) -> Dict[int, str]:
    """
    Re-extracción selectiva en modo ACCURATE de Docling para páginas dudosas.
    Configura una instancia secundaria cacheada como singleton con TableFormerMode.ACCURATE,
    images_scale=3.0 y OCR de página completa forzado.
    Procesa exclusivamente los page_numbers solicitados y registra la métrica
    'high_res_verification_pass' en telemetry.py.
    """
    if not page_numbers or not os.path.exists(pdf_path):
        return {}

    start_time = time.time()
    try:
        import psutil
        proc = psutil.Process()
        mem_before = proc.memory_info().rss / (1024 * 1024)
    except Exception:
        mem_before = 0.0

    converter = get_cached_converter(
        enable_ocr=True,
        accurate_tables=True,
        ocr_scale=3.0
    )

    result_pages: Dict[int, str] = {}
    for p in sorted(set(page_numbers)):
        try:
            res = converter.convert(pdf_path, page_range=(p, p))
            result_pages[p] = res.document.export_to_markdown()
        except Exception as e:
            result_pages[p] = f"<!-- Error re-extracting page {p}: {str(e)} -->"

    elapsed = round(time.time() - start_time, 2)
    try:
        mem_after = proc.memory_info().rss / (1024 * 1024)
        mem_used = max(0.0, mem_after - mem_before)
    except Exception:
        mem_used = 0.0

    from telemetry import record_high_res_verification
    record_high_res_verification(
        elapsed_seconds=elapsed,
        memory_mb=mem_used,
        pages_count=len(page_numbers)
    )

    return result_pages


def reconcile_extraction_passes(
    pass1_rows: List[Transaccion],
    pass2_rows: List[Transaccion],
    saldo_anterior_pagina: Optional[float] = None,
    modo_contable: str = "debito",
    tolerance: float = 0.01
) -> Tuple[List[Transaccion], Dict[str, Any]]:
    """
    Reconcilia y arbitra los resultados de extracción entre la Pasada 1 (FAST) y la Pasada 2 (ACCURATE).

    - Si la Pasada 2 logra que la cadena de saldo cuadre (diferencia <= tolerance),
      reemplaza las filas de la Pasada 1 en esa página, actualiza origen = 'verificado_multipass',
      recalcula confianza y cambia estado_revision = 'VERIFICADO'.
    - Si ambas pasadas difieren en importe, fecha o tipo y ninguna logra cuadrar la cadena de saldo,
      conserva ambas versiones en los metadatos de auditoría y marca la fila con
      estado_revision = 'DISCREPANCIA_OCR' y confianza = min(confianza, 0.40).
    """
    if not pass2_rows:
        return pass1_rows, {"status": "pass1_retained", "cadena_ok": False}

    # Evaluate pass 2 with running balance validation
    val_pass2, meta_pass2 = validate_running_balance_chain(
        [tx.model_copy() for tx in pass2_rows],
        saldo_inicial=saldo_anterior_pagina,
        modo_contable=modo_contable,
        tolerance=tolerance
    )

    pass2_cumple = (
        meta_pass2.get("filas_con_saldo", 0) > 0
        and meta_pass2.get("cumplimiento_cadena_saldo") == 100.0
    )

    if pass2_cumple:
        # Pass 2 squares the balance chain perfectly! Promote Pass 2 rows
        promoted_rows = []
        for tx in val_pass2:
            tx.origen = "verificado_multipass"
            compute_row_confidence(tx, cadena_ok=True, acuerdo_senales=True, fecha_en_periodo=True)
            tx.estado_revision = "VERIFICADO"
            promoted_rows.append(tx)
        return promoted_rows, {
            "status": "promoted_pass2",
            "cadena_ok": True,
            "cumplimiento_cadena_saldo": 100.0,
            "filas_reconciliadas": len(promoted_rows)
        }

    # Neither pass squares the running balance, check for discrepancies between pass 1 and pass 2
    reconciled_rows = []
    discrepancy_count = 0

    for i, tx1 in enumerate(pass1_rows):
        tx1_copy = tx1.model_copy()
        tx2 = pass2_rows[i] if i < len(pass2_rows) else None

        has_diff = False
        if tx2 is not None:
            tipo1 = tx1_copy.tipo.value if hasattr(tx1_copy.tipo, "value") else str(tx1_copy.tipo)
            tipo2 = tx2.tipo.value if hasattr(tx2.tipo, "value") else str(tx2.tipo)
            if (
                tx1_copy.fecha != tx2.fecha
                or abs(round(tx1_copy.importe - tx2.importe, 2)) > tolerance
                or tipo1.lower() != tipo2.lower()
            ):
                has_diff = True

        if has_diff and tx2 is not None:
            discrepancy_count += 1
            tipo2_str = tx2.tipo.value if hasattr(tx2.tipo, "value") else str(tx2.tipo)
            audit_note = f"[Discrepancia Pass 2: {tx2.fecha} | ${tx2.importe:.2f} | {tipo2_str}]"
            tx1_copy.descripcion = f"{tx1_copy.descripcion} {audit_note}".strip()
            tx1_copy.estado_revision = "DISCREPANCIA_OCR"
            tx1_copy.confianza = min(getattr(tx1_copy, "confianza", 1.0), 0.40)
        elif getattr(tx1_copy, "estado_revision", "") in ("REVISAR", "DISCREPANCIA_OCR"):
            tx1_copy.confianza = min(getattr(tx1_copy, "confianza", 1.0), 0.40)

        reconciled_rows.append(tx1_copy)

    return reconciled_rows, {
        "status": "discrepancia_ocr" if discrepancy_count > 0 else "unresolved",
        "cadena_ok": False,
        "discrepancies_detected": discrepancy_count
    }


