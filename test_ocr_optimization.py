"""
test_ocr_optimization.py - Unit & Integration Tests for Optimized PDF / OCR Module

Validates:
1. Heuristic PDF Profile Detection (digital text layer vs scanned image).
2. DocumentConverter Singleton Caching (zero cold-start on subsequent calls).
3. PDF Page Rendering for Visual Preview.
4. Targeted Page Range Conversion and Telemetry Metadata.
"""

import os
import tempfile
import unittest
import json
from unittest.mock import patch, MagicMock
from PIL import Image

from parsers import (
    detect_pdf_profile,
    render_pdf_page_image,
    get_cached_converter,
    parse_pdf,
    parse_xml,
    _CONVERTERS_CACHE,
    parse_docling_markdown_tables,
    validate_running_balance_chain,
    identify_low_confidence_pages,
    reextract_pages_high_res,
    reconcile_extraction_passes,
)
from models import Transaccion, TipoTransaccion, compute_row_confidence
from llm_client import (
    deduplicate_adjacent_chunks,
    deduplicate_all_chunks,
    deduplicate_within_chunk,
    parse_strict_llm_json,
    parse_heuristic_fallback,
    extract_transactions_with_ollama,
    is_candidate_row,
    evaluar_linea_candidata,
)


class TestOcrOptimization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Create a synthetic 2-page digital PDF
        cls.digital_pdf_content = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj
<< /Length 260 >>
stream
BT
/F1 12 Tf
72 720 Td (ESTADO DE CUENTA BANCARIO - PAGINA 1) Tj
0 -24 Td (Fecha | Concepto | Cargo | Abono | Saldo) Tj
0 -20 Td (15/01/2024 | DEPOSITO NOMINA EMPRESA SA | | 25000.00 | 25000.00) Tj
0 -20 Td (18/01/2024 | PAGO DE SERVICIO CFE | 1450.50 | | 23549.50) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
6 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
7 0 obj
<< /Length 220 >>
stream
BT
/F1 12 Tf
72 720 Td (ESTADO DE CUENTA BANCARIO - PAGINA 2) Tj
0 -24 Td (28/01/2024 | COMPRA WALMART SUPERCENTER | 3210.80 | | 20338.70) Tj
0 -20 Td (30/01/2024 | RETIRO CAJERO AUTOMATICO | 1000.00 | | 19338.70) Tj
ET
endstream
endobj
xref
0 8
0000000000 65535 f 
0000000010 00000 n 
0000000060 00000 n 
0000000123 00000 n 
0000000242 00000 n 
0000000555 00000 n 
0000000627 00000 n 
0000000746 00000 n 
trailer
<< /Size 8 /Root 1 0 R >>
startxref
1019
%%EOF"""

        cls.temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        cls.temp_file.write(cls.digital_pdf_content)
        cls.temp_file.close()
        cls.pdf_path = cls.temp_file.name

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(cls.pdf_path):
            os.unlink(cls.pdf_path)

    def test_detect_pdf_profile(self):
        """Verifies fast detection of pages and character density."""
        profile = detect_pdf_profile(self.pdf_path)
        self.assertEqual(profile["page_count"], 2)
        self.assertTrue(profile["is_digital_native"])
        self.assertEqual(profile["recommended_mode"], "digital")
        self.assertGreater(profile["avg_chars_per_page"], 50)
        self.assertEqual(len(profile["char_counts"]), 2)

    def test_render_pdf_page_image(self):
        """Verifies page rendering to PIL Image for visual preview."""
        img_p1 = render_pdf_page_image(self.pdf_path, 1, scale=0.5)
        self.assertIsInstance(img_p1, Image.Image)
        self.assertEqual(img_p1.size, (306, 396))

        img_p2 = render_pdf_page_image(self.pdf_path, 2, scale=1.0)
        self.assertIsInstance(img_p2, Image.Image)
        self.assertEqual(img_p2.size, (612, 792))

        # Test out of range error handling
        with self.assertRaises(ValueError):
            render_pdf_page_image(self.pdf_path, 99)

    def test_converter_caching(self):
        """Verifies that DocumentConverter instances are cached and reused."""
        _CONVERTERS_CACHE.clear()

        conv1 = get_cached_converter(enable_ocr=False, accurate_tables=False, num_threads=4)
        conv2 = get_cached_converter(enable_ocr=False, accurate_tables=False, num_threads=4)
        self.assertIs(conv1, conv2, "Converter instances with identical options must be identical (cached).")

        # Different settings create a different entry in cache
        conv3 = get_cached_converter(enable_ocr=True, accurate_tables=False, num_threads=4)
        self.assertIsNot(conv1, conv3)

    def test_parse_pdf_auto_detection_and_page_range(self):
        """Verifies that parse_pdf auto-detects digital mode and respects page ranges."""
        # Process page 1 only
        md_text, meta = parse_pdf(
            self.pdf_path,
            enable_ocr=None,  # auto-detect
            accurate_tables=False,
            page_range=(1, 1),
            num_threads=4,
        )

        self.assertIn("elapsed_seconds", meta)
        self.assertFalse(meta["effective_ocr"], "Auto-detect should disable OCR for native digital text.")
        self.assertEqual(meta["detection_reason"], "auto_detected")
        self.assertEqual(meta["pages_processed"], 1)
        self.assertEqual(meta["total_pages"], 2)
        self.assertIn("PAGINA 1", md_text)
        self.assertNotIn("PAGINA 2", md_text)

        # Process page 2 only
        md_text2, meta2 = parse_pdf(
            self.pdf_path,
            enable_ocr=None,
            page_range=(2, 2),
            num_threads=4,
        )
        self.assertEqual(meta2["pages_processed"], 1)
        self.assertIn("PAGINA 2", md_text2)
        self.assertNotIn("PAGINA 1", md_text2)

    def test_adjacent_chunk_deduplication(self):
        """
        Verifies that deduplicate_adjacent_chunks NEVER removes identical transactions
        at chunk boundaries unless both rows possess an explicit identical bank reference.
        """
        # 1. Non-destructive chunk boundary test: 2 identical purchases without reference must both be preserved
        tx1 = Transaccion(fecha="2024-01-10", concepto="Restaurante", importe=250.0, descripcion="Comida", tipo=TipoTransaccion.EGRESO)
        tx2 = Transaccion(fecha="2024-01-11", concepto="Gasolina", importe=600.0, descripcion="Carga combustible", tipo=TipoTransaccion.EGRESO)
        tx_boundary = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal despensa", tipo=TipoTransaccion.EGRESO)

        # Chunk 1 ends with tx_boundary
        chunk_1 = [tx1, tx2, tx_boundary]

        # Chunk 2 has an identical purchase at the start (legitimate twin purchase across non-overlapping chunks)
        tx_boundary_twin = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal despensa", tipo=TipoTransaccion.EGRESO)
        tx3 = Transaccion(fecha="2024-01-13", concepto="Farmacia", importe=180.0, descripcion="Medicamentos", tipo=TipoTransaccion.EGRESO)
        chunk_2 = [tx_boundary_twin, tx3]

        deduped_next = deduplicate_adjacent_chunks(chunk_1, chunk_2, boundary_window=3)
        # BOTH tx_boundary_twin and tx3 must be preserved since neither has an explicit bank reference!
        self.assertEqual(len(deduped_next), 2, "Identical purchases across chunk boundaries without reference must be preserved.")
        self.assertEqual(deduped_next[0].concepto, "Supermercado")
        self.assertEqual(deduped_next[1].concepto, "Farmacia")

        # 2. When identical bank references ARE present across chunk boundaries, deduplicate
        tx_ref_tail = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal Ref: 998877", tipo=TipoTransaccion.EGRESO)
        tx_ref_head = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal Ref: 998877", tipo=TipoTransaccion.EGRESO)
        deduped_with_refs = deduplicate_adjacent_chunks([tx_ref_tail], [tx_ref_head, tx3], boundary_window=3)
        self.assertEqual(len(deduped_with_refs), 1, "Boundary transactions with identical explicit bank reference should be deduplicated.")
        self.assertEqual(deduped_with_refs[0].concepto, "Farmacia")

        # 3. Legitimate same-day twins within same chunk (e.g. 2 identical coffees at same cafe without ref)
        tx_twin1 = Transaccion(fecha="2024-01-15", concepto="Cafetería", importe=65.0, descripcion="Café latte", tipo=TipoTransaccion.EGRESO)
        tx_twin2 = Transaccion(fecha="2024-01-15", concepto="Cafetería", importe=65.0, descripcion="Café latte", tipo=TipoTransaccion.EGRESO)
        twins_chunk = [tx_twin1, tx_twin2]

        preserved = deduplicate_within_chunk(twins_chunk)
        self.assertEqual(len(preserved), 2, "Identical transactions without explicit bank reference must both be preserved.")

        # 4. Duplicate transactions with explicit identical bank reference must be deduplicated
        tx_ref1 = Transaccion(fecha="2024-01-15", concepto="Pago", importe=100.0, descripcion="Pago servicio Ref: 12345678", tipo=TipoTransaccion.EGRESO)
        tx_ref2 = Transaccion(fecha="2024-01-15", concepto="Pago", importe=100.0, descripcion="Pago servicio Ref: 12345678", tipo=TipoTransaccion.EGRESO)
        ref_chunk = [tx_ref1, tx_ref2]

        deduped_refs = deduplicate_within_chunk(ref_chunk)
        self.assertEqual(len(deduped_refs), 1, "Transactions with identical explicit bank reference should be deduplicated.")

        # 5. Multi-chunk end-to-end pipeline: 3 from chunk 1 + 2 from chunk 2 + 2 from twins chunk = 7
        all_deduped = deduplicate_all_chunks([chunk_1, chunk_2, twins_chunk])
        self.assertEqual(len(all_deduped), 7)

    def test_secure_xml_cfdi_parsing_fixtures(self):
        """
        Verifies secure parsing of Mexican CFDI XML fixtures (credit card and debit card)
        with etree.XMLParser(resolve_entities=False, no_network=True, recover=True).
        """
        fixtures_dir = os.path.join(os.path.dirname(__file__), "tests", "fixtures")
        credito_path = os.path.join(fixtures_dir, "dummy_xml_credito.xml")
        debito_path = os.path.join(fixtures_dir, "dummy_xml_debito.xml")

        self.assertTrue(os.path.exists(credito_path), f"Fixture missing: {credito_path}")
        self.assertTrue(os.path.exists(debito_path), f"Fixture missing: {debito_path}")

        # 1. Test credit card XML fixture
        txs_cred, raw_cred = parse_xml(credito_path)
        self.assertIsNotNone(txs_cred, "dummy_xml_credito.xml should be parsed deterministically")
        self.assertGreater(len(txs_cred), 0)
        # Verify movements contain expected amounts and types
        importes_cred = [tx.importe for tx in txs_cred]
        self.assertIn(35000.0, importes_cred)
        self.assertIn(4000.0, importes_cred)
        self.assertIn(755.0, importes_cred)
        for tx in txs_cred:
            self.assertIn(tx.tipo, (TipoTransaccion.INGRESO, TipoTransaccion.EGRESO))
            self.assertRegex(tx.fecha, r"^\d{4}-\d{2}-\d{2}$")

        # 2. Test debit card XML fixture
        txs_deb, raw_deb = parse_xml(debito_path)
        self.assertIsNotNone(txs_deb, "dummy_xml_debito.xml should be parsed deterministically")
        self.assertEqual(len(txs_deb), 3)
        importes_deb = [tx.importe for tx in txs_deb]
        self.assertEqual(importes_deb, [1000.0, 2000.0, 3000.0])
        for tx in txs_deb:
            self.assertIn(tx.tipo, (TipoTransaccion.INGRESO, TipoTransaccion.EGRESO))
            self.assertRegex(tx.fecha, r"^\d{4}-\d{2}-\d{2}$")

    @patch("httpx.Client.post")
    def test_ollama_json_and_heuristic_fallback_execution(self, mock_post):
        """
        Simulates valid and malformed JSON responses from Ollama,
        verifying that parse_strict_llm_json and parse_heuristic_fallback
        execute without NameError.
        """
        # 1. Direct function execution test without NameError
        valid_json = json.dumps({
            "transacciones": [
                {
                    "fecha": "2024-01-15",
                    "concepto": "Supermercado",
                    "importe": 450.50,
                    "tipo": "egreso",
                    "descripcion": "Compra semanal Supermercado"
                }
            ]
        })
        strict_txs = parse_strict_llm_json(valid_json)
        self.assertEqual(len(strict_txs), 1)
        self.assertEqual(strict_txs[0].concepto, "Supermercado")
        self.assertEqual(strict_txs[0].importe, 450.50)

        # Fallback test with markdown/tabular text
        table_chunk = "| 18/01/2024 | PAGO DE SERVICIO CFE | 1450.50 | | 23549.50 |"
        fallback_txs = parse_heuristic_fallback(table_chunk)
        self.assertEqual(len(fallback_txs), 1)
        self.assertEqual(fallback_txs[0].importe, 1450.50)
        self.assertEqual(fallback_txs[0].tipo, TipoTransaccion.EGRESO)

        # 2. Mocked Ollama end-to-end flow with valid response
        mock_resp_valid = MagicMock()
        mock_resp_valid.status_code = 200
        mock_resp_valid.json.return_value = {
            "message": {
                "role": "assistant",
                "content": valid_json
            }
        }
        mock_resp_valid.raise_for_status.return_value = None
        mock_post.return_value = mock_resp_valid

        sample_text = "| 15/01/2024 | Supermercado | 450.50 |"
        txs_from_ollama = extract_transactions_with_ollama(sample_text)
        self.assertEqual(len(txs_from_ollama), 1)
        self.assertEqual(txs_from_ollama[0].concepto, "Supermercado")

        # 3. Mocked Ollama end-to-end flow with malformed response invoking fallback
        mock_resp_malformed = MagicMock()
        mock_resp_malformed.status_code = 200
        mock_resp_malformed.json.return_value = {
            "message": {
                "role": "assistant",
                "content": "MALFORMED_OUTPUT_NOT_JSON: " + table_chunk
            }
        }
        mock_resp_malformed.raise_for_status.return_value = None
        mock_post.return_value = mock_resp_malformed

        txs_fallback_end_to_end = extract_transactions_with_ollama(sample_text)
        self.assertGreaterEqual(len(txs_fallback_end_to_end), 1)

    def test_fase1_capture_and_rejections(self):
        """
        Verifies:
        a) Rows with dates '15 ENE', '15/ENE', '15/01' marked as candidates (candidata=True).
        b) 2 identical purchases on chunk boundary without repeated reference are preserved.
        c) Invalid rows are recorded in filas_rechazadas with reason and do not disappear silently.
        """
        # a) Candidate dates verification
        lines = [
            "| 15 ENE | COMPRA OXXO | 120.00 |",
            "| 15/ENE | FARMACIA GUADALAJARA | 450.50 |",
            "| 15/01 | CAFENIO DRIVE THRU | 85.00 |",
        ]
        for line in lines:
            self.assertTrue(is_candidate_row(line), f"Line should be candidate: {line}")
            res = evaluar_linea_candidata(line)
            self.assertTrue(res["candidata"], f"Line should have candidata=True: {line}")

        # b) 2 identical purchases at chunk boundary without repeated reference
        tx_a = Transaccion(fecha="2024-01-15", concepto="Cafeteria", importe=60.0, descripcion="Cafe americano", tipo=TipoTransaccion.EGRESO)
        tx_b = Transaccion(fecha="2024-01-15", concepto="Cafeteria", importe=60.0, descripcion="Cafe americano", tipo=TipoTransaccion.EGRESO)
        res_chunks = deduplicate_adjacent_chunks([tx_a], [tx_b])
        self.assertEqual(len(res_chunks), 1, "Next chunk transaction must be preserved (not dropped)")
        self.assertEqual(res_chunks[0].importe, 60.0)

        # c) Rejection auditing
        filas_rechazadas = []
        raw_json_with_invalids = json.dumps({
            "transacciones": [
                # Valid row
                {"fecha": "2024-01-15", "concepto": "Valida", "importe": 100.0, "tipo": "cargo", "descripcion": "Valida"},
                # Invalid schema row
                {"fecha": "", "concepto": "Sin Fecha", "importe": 200.0, "tipo": "cargo", "descripcion": "Sin Fecha"},
                # Invalid zero amount row
                {"fecha": "2024-01-15", "concepto": "Importe Cero", "importe": 0.0, "tipo": "abono", "descripcion": "Importe Cero"},
            ]
        })
        parsed_txs = parse_strict_llm_json(raw_json_with_invalids, filas_rechazadas=filas_rechazadas)
        self.assertEqual(len(parsed_txs), 1)
        self.assertEqual(len(filas_rechazadas), 2)
        motivos = [r["motivo"] for r in filas_rechazadas]
        self.assertIn("fecha_invalida", motivos)
        self.assertIn("importe_cero", motivos)

    def test_fase2_deterministic_markdown_tables_and_confidence(self):
        """
        Fase 2 Comprehensive Validation:
        1. Parse banking Markdown table with columns | Fecha | Concepto | Retiros | Depósitos | Saldo |
           including multiline descriptions continuing in lower row without date/amount.
        2. Detect shifted cell where a charge appears in deposit column, but Saldo decreases:
           verifies validate_running_balance_chain inverts type, sets REVISAR, and audits adjustment.
        3. compute_row_confidence: verifies valid table balance chain achieves 1.0 (VERIFICADO)
           and broken balance chain drops to < 0.75 (REVISAR).
        """
        # 1. Markdown table with multiline description continuation
        markdown_table = """
# Resumen de Movimientos
| Fecha | Concepto | Retiros | Depósitos | Saldo |
|---|---|---|---|---|
| 2024-05-10 | DEPOSITO NOMINA | | 15,000.00 | 25,000.00 |
| 2024-05-11 | COMPRA TIENDA DEPARTAMENTAL | 1,250.00 | | 23,750.00 |
| | SUCURSAL CENTRO HERMOSILLO | | | |
| 2024-05-12 | PAGO SERVICIO CFE | 450.00 | | 23,300.00 |
"""
        txs, unparsed, meta = parse_docling_markdown_tables(markdown_table, modo_contable="debito")
        self.assertEqual(len(txs), 3, "Must extract exactly 3 transactions (multiline continuation merged)")
        self.assertEqual(txs[0].tipo, TipoTransaccion.INGRESO)
        self.assertEqual(txs[0].importe, 15000.00)
        self.assertEqual(txs[0].saldo_renglon, 25000.00)

        # Verify multiline merging
        self.assertEqual(txs[1].tipo, TipoTransaccion.EGRESO)
        self.assertEqual(txs[1].importe, 1250.00)
        self.assertIn("SUCURSAL CENTRO HERMOSILLO", txs[1].descripcion)
        self.assertIn("SUCURSAL CENTRO HERMOSILLO", txs[1].linea_origen)

        self.assertEqual(txs[2].tipo, TipoTransaccion.EGRESO)
        self.assertEqual(txs[2].importe, 450.00)
        self.assertEqual(len(unparsed), 0, "All candidate rows belong to the structured table")

        # 2. Sequential balance chain validation on the valid table
        val_txs, chain_meta = validate_running_balance_chain(
            txs,
            saldo_inicial=10000.00,
            modo_contable="debito"
        )
        self.assertEqual(chain_meta["filas_con_saldo"], 3)
        self.assertEqual(chain_meta["filas_cumplen_cadena"], 3)
        self.assertEqual(chain_meta["cumplimiento_cadena_saldo"], 100.0)
        self.assertEqual(chain_meta["filas_corregidas"], 0)
        for tx in val_txs:
            self.assertEqual(tx.confianza, 1.0)
            self.assertEqual(tx.estado_revision, "VERIFICADO")

        # 3. Detect shifted cell where withdrawal is placed in deposit column but Saldo decreases
        # Opening balance: 23,300.00. Withdrawal of 1,000.00 erroneously placed in Depósitos, Saldo = 22,300.00.
        shifted_markdown = """
| Fecha | Concepto | Retiros | Depósitos | Saldo |
|---|---|---|---|---|
| 2024-05-13 | DISPOSICION CAJERO | | 1,000.00 | 22,300.00 |
"""
        shifted_txs, _, _ = parse_docling_markdown_tables(shifted_markdown, modo_contable="debito")
        self.assertEqual(len(shifted_txs), 1)
        self.assertEqual(shifted_txs[0].tipo, TipoTransaccion.INGRESO, "Initially parsed as deposit from column")

        corrected_txs, shift_meta = validate_running_balance_chain(
            shifted_txs,
            saldo_inicial=23300.00,
            modo_contable="debito"
        )
        self.assertEqual(shift_meta["filas_corregidas"], 1, "Must correct shifted cell")
        self.assertEqual(corrected_txs[0].tipo, TipoTransaccion.EGRESO, "Tipo inverted to EGRESO by balance equation")
        self.assertEqual(corrected_txs[0].estado_revision, "REVISAR", "Shifted cell must remain flagged for review")
        self.assertIn("[Ajuste: tipo invertido por saldo renglón]", corrected_txs[0].descripcion)

        # 4. compute_row_confidence mathematical unit properties
        # Table row with valid balance chain: 0.85 + 0.15 = 1.0 -> VERIFICADO
        tx_valid = Transaccion(fecha="2024-05-10", concepto="Abono", importe=500.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO, origen="determinista_tabla")
        conf_val = compute_row_confidence(tx_valid, cadena_ok=True, acuerdo_senales=True, fecha_en_periodo=True)
        self.assertEqual(conf_val, 1.0)
        self.assertEqual(tx_valid.estado_revision, "VERIFICADO")

        # Table row with broken balance chain: 0.85 - 0.25 = 0.60 < 0.75 -> REVISAR
        tx_broken = Transaccion(fecha="2024-05-10", concepto="Cargo", importe=200.0, descripcion="Cargo", tipo=TipoTransaccion.EGRESO, origen="determinista_tabla")
        conf_broken = compute_row_confidence(tx_broken, cadena_ok=False, acuerdo_senales=True, fecha_en_periodo=True)
        self.assertEqual(conf_broken, 0.60)
        self.assertLess(conf_broken, 0.75)
        self.assertEqual(tx_broken.estado_revision, "REVISAR")

    def test_identify_low_confidence_pages_selection(self):
        """
        Verifies that identify_low_confidence_pages:
        a) Identifies uniquely the page with broken running balance chain (e.g. page 2 of 5).
        b) Flags pages with confianza < 0.75 or estado_revision == 'REVISAR'.
        c) Flags pages present in filas_rechazadas.
        d) Flags pages when page coverage < 0.99.
        """
        # Create 5 pages of transactions
        # Page 1: OK (1000 - 100 = 900)
        tx_p1 = Transaccion(fecha="2024-01-01", concepto="Pago 1", importe=100.0, descripcion="Pago 1", tipo=TipoTransaccion.EGRESO, saldo_renglon=900.0, pagina=1)
        # Page 2: Broken balance (900 - 200 != 500)
        tx_p2 = Transaccion(fecha="2024-01-02", concepto="Pago 2", importe=200.0, descripcion="Pago 2", tipo=TipoTransaccion.EGRESO, saldo_renglon=500.0, pagina=2)
        # Page 3: OK from 500 (500 - 50 = 450)
        tx_p3 = Transaccion(fecha="2024-01-03", concepto="Pago 3", importe=50.0, descripcion="Pago 3", tipo=TipoTransaccion.EGRESO, saldo_renglon=450.0, pagina=3)
        # Page 4: OK (450 + 1000 = 1450)
        tx_p4 = Transaccion(fecha="2024-01-04", concepto="Abono 4", importe=1000.0, descripcion="Abono 4", tipo=TipoTransaccion.INGRESO, saldo_renglon=1450.0, pagina=4)
        # Page 5: OK (1450 - 150 = 1300)
        tx_p5 = Transaccion(fecha="2024-01-05", concepto="Pago 5", importe=150.0, descripcion="Pago 5", tipo=TipoTransaccion.EGRESO, saldo_renglon=1300.0, pagina=5)

        txs = [tx_p1, tx_p2, tx_p3, tx_p4, tx_p5]

        # a) Only page 2 has balance chain rupture
        pages_detected = identify_low_confidence_pages(txs, filas_rechazadas=None, cobertura=1.0, modo_contable="debito")
        self.assertEqual(pages_detected, [2], "Must select solely page 2 with broken balance chain")

        # b) Row with low confidence or REVISAR
        tx_p1_rev = tx_p1.model_copy()
        tx_p1_rev.confianza = 0.65
        tx_p1_rev.estado_revision = "REVISAR"
        pages_rev = identify_low_confidence_pages([tx_p1_rev, tx_p3, tx_p4], filas_rechazadas=None, cobertura=1.0)
        self.assertIn(1, pages_rev)

        # c) Rejected row on page 4
        rejections = [{"pagina": 4, "motivo": "importe_invalido", "linea": "| 15/01 | Error |"}]
        pages_with_rej = identify_low_confidence_pages([tx_p1, tx_p3], filas_rechazadas=rejections, cobertura=1.0)
        self.assertIn(4, pages_with_rej)

        # d) Cobertura < 0.99 adds transaction pages
        pages_cov = identify_low_confidence_pages([tx_p1, tx_p3], filas_rechazadas=None, cobertura=0.95)
        self.assertEqual(pages_cov, [1, 3])

    def test_reconcile_extraction_passes_promotion_and_discrepancy(self):
        """
        Verifies reconcile_extraction_passes:
        a) If Pass 2 (ACCURATE) squares running balance chain, replaces Pass 1 rows,
           updates origen = 'verificado_multipass', and sets estado_revision = 'VERIFICADO'.
        b) If both passes differ and neither squares running balance, marks row as
           'DISCREPANCIA_OCR' with confianza <= 0.40 and audit annotation.
        """
        # a) Promotion scenario:
        # Pass 1 had bad OCR amount: 150.0 instead of 100.0, so 1000 - 150 != 900
        pass1_bad = [
            Transaccion(fecha="2024-01-10", concepto="Compra", importe=150.0, descripcion="Compra", tipo=TipoTransaccion.EGRESO, saldo_renglon=900.0, pagina=1)
        ]
        # Pass 2 correctly recognized 100.0, squaring 1000 - 100 == 900
        pass2_good = [
            Transaccion(fecha="2024-01-10", concepto="Compra", importe=100.0, descripcion="Compra", tipo=TipoTransaccion.EGRESO, saldo_renglon=900.0, pagina=1)
        ]

        reconciled_ok, meta_ok = reconcile_extraction_passes(
            pass1_rows=pass1_bad,
            pass2_rows=pass2_good,
            saldo_anterior_pagina=1000.0,
            modo_contable="debito"
        )
        self.assertEqual(meta_ok["status"], "promoted_pass2")
        self.assertTrue(meta_ok["cadena_ok"])
        self.assertEqual(len(reconciled_ok), 1)
        self.assertEqual(reconciled_ok[0].origen, "verificado_multipass")
        self.assertEqual(reconciled_ok[0].estado_revision, "VERIFICADO")
        self.assertEqual(reconciled_ok[0].importe, 100.0)
        self.assertEqual(reconciled_ok[0].confianza, 1.0)

        # b) Discrepancy scenario:
        # Pass 1: 150.0, Pass 2: 250.0. Neither squares against saldo_anterior = 1000 and saldo_renglon = 500
        pass1_err = [
            Transaccion(fecha="2024-01-10", concepto="Servicio", importe=150.0, descripcion="Servicio", tipo=TipoTransaccion.EGRESO, saldo_renglon=500.0, pagina=2, confianza=0.8)
        ]
        pass2_err = [
            Transaccion(fecha="2024-01-10", concepto="Servicio", importe=250.0, descripcion="Servicio", tipo=TipoTransaccion.EGRESO, saldo_renglon=500.0, pagina=2, confianza=0.8)
        ]

        reconciled_disc, meta_disc = reconcile_extraction_passes(
            pass1_rows=pass1_err,
            pass2_rows=pass2_err,
            saldo_anterior_pagina=1000.0,
            modo_contable="debito"
        )
        self.assertEqual(meta_disc["status"], "discrepancia_ocr")
        self.assertFalse(meta_disc["cadena_ok"])
        self.assertEqual(len(reconciled_disc), 1)
        self.assertEqual(reconciled_disc[0].estado_revision, "DISCREPANCIA_OCR")
        self.assertLessEqual(reconciled_disc[0].confianza, 0.40)
        self.assertIn("[Discrepancia Pass 2:", reconciled_disc[0].descripcion)


if __name__ == "__main__":
    unittest.main()
