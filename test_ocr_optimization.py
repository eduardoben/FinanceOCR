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
    _CONVERTERS_CACHE,
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


if __name__ == "__main__":
    unittest.main()
