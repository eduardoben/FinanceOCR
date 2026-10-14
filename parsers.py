"""
parsers.py - Document and XML Parsers for Bank Statements

Provides layout-aware PDF parsing via Docling (OCR & TableFormer)
with intelligent hybrid pre-flight inspection (pypdfium2),
singleton converter caching, dynamic multithreading, and
deterministic Mexican CFDI / XML parsing via lxml.
"""

import os
import time
import functools
import multiprocessing
from typing import List, Tuple, Optional, Dict, Any
from PIL import Image
from lxml import etree
import pypdfium2 as pdfium

from models import Transaccion, TipoTransaccion
from date_utils import normalize_spanish_date

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

# Global converter instance registry to eliminate cold-start overhead
_CONVERTERS_CACHE: Dict[Tuple[bool, bool, int, float], DocumentConverter] = {}


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

        return {
            "page_count": total_pages,
            "char_counts": char_counts,
            "avg_chars_per_page": round(avg_chars, 1),
            "is_digital_native": is_digital,
            "recommended_mode": "digital" if is_digital else "ocr",
        }
    except Exception as ex:
        # Graceful fallback for protected or non-standard PDF formats
        return {
            "page_count": 1,
            "char_counts": [0],
            "avg_chars_per_page": 0.0,
            "is_digital_native": False,
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
    accelerator and OCR pipeline options, preventing repeated model initialization.
    """
    if num_threads is None:
        cpu_cores = multiprocessing.cpu_count() or 4
        num_threads = min(cpu_cores, 12)

    cache_key = (enable_ocr, accurate_tables, num_threads, ocr_scale)
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

    # Determine effective OCR mode
    if enable_ocr is None:
        # Auto-detect: if document has native text, disable OCR for 10x speedup
        effective_ocr = not profile["is_digital_native"]
        detection_reason = "auto_detected"
    else:
        effective_ocr = bool(enable_ocr)
        detection_reason = "user_override"

    # Validate and normalize page_range
    effective_page_range: Optional[Tuple[int, int]] = None
    if page_range:
        p_start = max(1, min(page_range[0], total_pages))
        p_end = max(p_start, min(page_range[1], total_pages))
        effective_page_range = (p_start, p_end)

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
    r"\b(?:\d{1,2}[-/\s](?:ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic|[a-z]{3}|\d{1,2})[-/\s]\d{2,4}|\d{4}[-/\s]\d{1,2}[-/\s]\d{1,2}|\d{8})\b",
    re.IGNORECASE
)
MONEY_PATTERN = re.compile(r"(?:\$\s*[\d,]+\.\d{2}|[\d,]+\.\d{2})")


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

    parser = etree.XMLParser(recover=True, remove_comments=True)
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

        expense_kw = [
            "CGO", "CARGO", "RETIRO", "DISPOSICION", "CAJERO",
            "PAGO DE TARJETA", "PAGO TARJETA", "PAGO SERVICIO", "PAGO DE SERVICIOS",
            "PORTABILIDAD", "PORTABILIDAD DE NOMINA",
            "TRANSFERENCIA A", "TRANSFERENCIA INTERBANCARIA", "SPEI ENVIADO",
            "TRASPASO A", "COMPRA", "CONSUMO", "COMISION", "IVA", "VALIDACION"
        ]
        income_kw = [
            "SU PAGO GRACIAS", "PAGO GRACIAS", "SU PAGO",
            "NOMINA", "SUELDO", "HONORARIOS",
            "PAGO DE BECA", "DEPOSITO BECA", "BECA",
            "HACIENDA TE DEVUELVE", "DEVOLUCION", "REEMBOLSO",
            "BONIFICACION", "CASHBACK", "2NOW", "RECOMPENSA", "SALDO A FAVOR",
            "ABONO", "ABO ", "DEPOSITO", "DEP ",
            "SPEI RECIBIDO", "TRANSFERENCIA RECIBIDA", "TRASPASO RECIBIDO",
            "INTERESES GANADOS", "RENDIMIENTOS"
        ]

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
                if "MERCADOPAGO" in d_upper or "MERCADO PAGO" in d_upper:
                    m_flow = TipoTransaccion.EGRESO
                else:
                    m_flow = None
                    for kw in expense_kw:
                        if kw in d_upper:
                            m_flow = TipoTransaccion.EGRESO
                            break
                    if not m_flow:
                        for kw in income_kw:
                            if kw in d_upper:
                                m_flow = TipoTransaccion.INGRESO
                                break
                    if not m_flow:
                        if is_credit_card:
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
                has_global_taxes = bool(comp.xpath(".//*[local-name()='Impuestos']"))
                imp_final = total if has_global_taxes else imp_sub
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

