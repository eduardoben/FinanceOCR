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
from PIL import Image

from parsers import (
    detect_pdf_profile,
    render_pdf_page_image,
    get_cached_converter,
    parse_pdf,
    parse_xml,
    _CONVERTERS_CACHE,
)
from models import Transaccion, TipoTransaccion
from llm_client import (
    deduplicate_adjacent_chunks,
    deduplicate_all_chunks,
    deduplicate_within_chunk,
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
        Verifies that deduplicate_adjacent_chunks removes overlapping row duplicates
        at chunk boundaries (tail of chunk_i vs head of chunk_i+1), while preserving
        legitimate identical twin transactions within the same chunk unless they share
        an explicit bank reference.
        """
        # 1. Overlapping boundary test
        tx1 = Transaccion(fecha="2024-01-10", concepto="Restaurante", importe=250.0, descripcion="Comida", tipo=TipoTransaccion.EGRESO)
        tx2 = Transaccion(fecha="2024-01-11", concepto="Gasolina", importe=600.0, descripcion="Carga combustible", tipo=TipoTransaccion.EGRESO)
        tx_boundary = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal despensa", tipo=TipoTransaccion.EGRESO)

        # Chunk 1 ends with tx_boundary
        chunk_1 = [tx1, tx2, tx_boundary]

        # Chunk 2 repeats tx_boundary at the start due to table chunk overlap
        tx_boundary_dup = Transaccion(fecha="2024-01-12", concepto="Supermercado", importe=1250.50, descripcion="Compra semanal despensa", tipo=TipoTransaccion.EGRESO)
        tx3 = Transaccion(fecha="2024-01-13", concepto="Farmacia", importe=180.0, descripcion="Medicamentos", tipo=TipoTransaccion.EGRESO)
        chunk_2 = [tx_boundary_dup, tx3]

        deduped_next = deduplicate_adjacent_chunks(chunk_1, chunk_2, boundary_window=3)
        # tx_boundary_dup must be removed, leaving only tx3
        self.assertEqual(len(deduped_next), 1)
        self.assertEqual(deduped_next[0].concepto, "Farmacia")

        # 2. Legitimate same-day twins within same chunk (e.g. 2 identical coffees at same cafe without ref)
        tx_twin1 = Transaccion(fecha="2024-01-15", concepto="Cafetería", importe=65.0, descripcion="Café latte", tipo=TipoTransaccion.EGRESO)
        tx_twin2 = Transaccion(fecha="2024-01-15", concepto="Cafetería", importe=65.0, descripcion="Café latte", tipo=TipoTransaccion.EGRESO)
        twins_chunk = [tx_twin1, tx_twin2]

        preserved = deduplicate_within_chunk(twins_chunk)
        self.assertEqual(len(preserved), 2, "Identical transactions without explicit bank reference must both be preserved.")

        # 3. Duplicate transactions with explicit identical bank reference must be deduplicated
        tx_ref1 = Transaccion(fecha="2024-01-15", concepto="Pago", importe=100.0, descripcion="Pago servicio Ref: 12345678", tipo=TipoTransaccion.EGRESO)
        tx_ref2 = Transaccion(fecha="2024-01-15", concepto="Pago", importe=100.0, descripcion="Pago servicio Ref: 12345678", tipo=TipoTransaccion.EGRESO)
        ref_chunk = [tx_ref1, tx_ref2]

        deduped_refs = deduplicate_within_chunk(ref_chunk)
        self.assertEqual(len(deduped_refs), 1, "Transactions with identical explicit bank reference should be deduplicated.")

        # 4. Multi-chunk end-to-end pipeline
        all_deduped = deduplicate_all_chunks([chunk_1, chunk_2, twins_chunk])
        self.assertEqual(len(all_deduped), 6)  # 3 from chunk 1 + 1 from chunk 2 + 2 from twins chunk

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


if __name__ == "__main__":
    unittest.main()
