"""
test_data_validation.py - Comprehensive Data Validation & Integrity Test Suite

Validates:
1. Pydantic model boundary conditions, amount sanitization, and required field validation
2. Enum strictness for TipoTransaccion
3. SQLite relational integrity, CHECK constraints, and foreign key enforcement
4. Transactional atomic rollback on batch errors
5. SQL injection resilience across search filters
6. Spanish banking date normalization matrix (all 12 months, cases, and formats)
7. Export data integrity: Excel workbook structure, CSV UTF-8 BOM, and TSV formatting
"""

import os
import io
import gc
import sqlite3
import tempfile
import unittest
import pandas as pd
from pydantic import ValidationError

from models import (
    Transaccion,
    TipoTransaccion,
    TransaccionesResponse,
    ReconciliacionResultado,
    sanitize_importe,
    validar_reconciliacion,
)
from date_utils import normalize_spanish_date, resolve_year_for_month, extract_statement_period
from database import (
    db_session,
    init_db,
    save_processed_transactions,
    query_transactions,
    compute_file_hash,
)
from utils import (
    create_standard_dataframe,
    df_to_excel_bytes,
    df_to_tsv,
    df_to_csv_bytes,
)


class TestDataValidationAndIntegrity(unittest.TestCase):

    def setUp(self):
        """Creates an isolated temporary database for relational constraint tests."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_val_finanzas.db")
        init_db(self.db_path)

    def tearDown(self):
        """Cleans up temporary directory after each test."""
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # -------------------------------------------------------------
    # 1. PYDANTIC SCHEMA & AMOUNT SANITIZATION TESTS
    # -------------------------------------------------------------
    def test_amount_sanitization_matrix(self):
        """Verifies thorough amount sanitization across varied currency representations."""
        test_matrix = [
            ("$ 1,234.56", 1234.56),
            ("1,234.56 MXN", 1234.56),
            ("USD $500.00", 500.00),
            ("-1,500.75", 1500.75),
            ("+$250.00", 250.00),
            (123, 123.0),
            (456.78, 456.78),
            ("-0.00", 0.0),
            ("  $  99.99  ", 99.99),
            ("invalid_text", 0.0),
            ("", 0.0),
            (None, 0.0),
        ]

        for raw_val, expected in test_matrix:
            tx = Transaccion(
                fecha="2024-05-01",
                concepto="Test Merchant",
                importe=raw_val,
                descripcion="Detalle de prueba",
                tipo=TipoTransaccion.EGRESO
            )
            self.assertAlmostEqual(
                tx.importe,
                expected,
                places=2,
                msg=f"Failed to sanitize raw amount: {raw_val}"
            )

    def test_tipo_transaccion_enum_enforcement(self):
        """Verifies that only valid 'ingreso' or 'egreso' values are accepted."""
        # Valid cases
        tx_ing = Transaccion(fecha="2024-01-01", concepto="Abono", importe=100.0, descripcion="OK", tipo="ingreso")
        self.assertEqual(tx_ing.tipo, TipoTransaccion.INGRESO)

        tx_eg = Transaccion(fecha="2024-01-01", concepto="Cargo", importe=50.0, descripcion="OK", tipo="egreso")
        self.assertEqual(tx_eg.tipo, TipoTransaccion.EGRESO)

        # Invalid cases must raise ValidationError
        invalid_types = ["gasto", "cargo", "abono", "deposito", "otro", "", 123]
        for inv in invalid_types:
            with self.assertRaises(ValidationError, msg=f"Should reject invalid tipo: {inv}"):
                Transaccion(
                    fecha="2024-01-01",
                    concepto="Fail",
                    importe=100.0,
                    descripcion="Fail",
                    tipo=inv
                )

    def test_required_fields_validation(self):
        """Verifies that missing required fields trigger ValidationError."""
        # Missing fecha
        with self.assertRaises(ValidationError):
            Transaccion(concepto="X", importe=10.0, descripcion="D", tipo=TipoTransaccion.EGRESO)

        # Missing concepto
        with self.assertRaises(ValidationError):
            Transaccion(fecha="2024-01-01", importe=10.0, descripcion="D", tipo=TipoTransaccion.EGRESO)

        # Missing importe
        with self.assertRaises(ValidationError):
            Transaccion(fecha="2024-01-01", concepto="X", descripcion="D", tipo=TipoTransaccion.EGRESO)

        # Missing tipo
        with self.assertRaises(ValidationError):
            Transaccion(fecha="2024-01-01", concepto="X", importe=10.0, descripcion="D")

    # -------------------------------------------------------------
    # 2. SQLITE RELATIONAL INTEGRITY & CONSTRAINT TESTS
    # -------------------------------------------------------------
    def test_sqlite_check_constraint_enforcement(self):
        """Verifies that SQLite CHECK(tipo IN ('ingreso', 'egreso')) constraint rejects illegal values."""
        with self.assertRaises(sqlite3.IntegrityError):
            with db_session(self.db_path) as conn:
                # Insert parent file first
                cursor = conn.execute(
                    """
                    INSERT INTO archivos_procesados (
                        nombre_archivo, hash_sha256, tipo_archivo, fecha_procesamiento
                    ) VALUES ('test.pdf', 'hash1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef', 'pdf', '2024-01-01 10:00:00');
                    """
                )
                file_id = cursor.lastrowid
                # Attempt to insert illegal tipo directly via SQL
                conn.execute(
                    """
                    INSERT INTO transacciones (archivo_id, fecha, concepto, importe, descripcion, tipo)
                    VALUES (?, '2024-01-01', 'Test', 100.0, 'Desc', 'invalido');
                    """,
                    (file_id,)
                )

    def test_sqlite_foreign_key_constraint_enforcement(self):
        """Verifies that inserting a transaction with an invalid archivo_id raises IntegrityError."""
        with self.assertRaises(sqlite3.IntegrityError):
            with db_session(self.db_path) as conn:
                # archivo_id 99999 does not exist
                conn.execute(
                    """
                    INSERT INTO transacciones (archivo_id, fecha, concepto, importe, descripcion, tipo)
                    VALUES (99999, '2024-01-01', 'Test', 100.0, 'Desc', 'ingreso');
                    """
                )

    def test_sqlite_unique_hash_constraint(self):
        """Verifies that inserting duplicate file hashes without overwrite raises IntegrityError."""
        file_hash = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        txs = [Transaccion(fecha="2024-01-01", concepto="Test", importe=100.0, descripcion="D", tipo=TipoTransaccion.INGRESO)]

        # First insert succeeds
        save_processed_transactions("file1.pdf", file_hash, "pdf", txs, overwrite=False, db_path=self.db_path)

        # Second insert with same hash and overwrite=False must fail
        with self.assertRaises(sqlite3.IntegrityError):
            with db_session(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO archivos_procesados (nombre_archivo, hash_sha256, tipo_archivo, fecha_procesamiento)
                    VALUES ('file2.pdf', ?, 'pdf', '2024-01-02 10:00:00');
                    """,
                    (file_hash,)
                )

    def test_atomic_rollback_on_batch_error(self):
        """Verifies that an error during batch saving rolls back the entire transaction, leaving no orphans."""
        file_hash = compute_file_hash(b"test batch rollback")

        # Mock a situation where transaction fails mid-way
        with self.assertRaises(Exception):
            with db_session(self.db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO archivos_procesados (nombre_archivo, hash_sha256, tipo_archivo, fecha_procesamiento)
                    VALUES ('rollback_test.pdf', ?, 'pdf', '2024-01-01 10:00:00');
                    """,
                    (file_hash,)
                )
                file_id = cursor.lastrowid
                conn.execute(
                    """
                    INSERT INTO transacciones (archivo_id, fecha, concepto, importe, descripcion, tipo)
                    VALUES (?, '2024-01-01', 'Tx1', 100.0, 'D1', 'ingreso');
                    """,
                    (file_id,)
                )
                # Intentionally trigger an error
                conn.execute("INSERT INTO transacciones_inexistente VALUES (1);")

        # Verify nothing was committed
        with db_session(self.db_path) as conn:
            files_count = conn.execute("SELECT COUNT(*) FROM archivos_procesados WHERE hash_sha256 = ?;", (file_hash,)).fetchone()[0]
            tx_count = conn.execute("SELECT COUNT(*) FROM transacciones;").fetchone()[0]
            self.assertEqual(files_count, 0)
            self.assertEqual(tx_count, 0)

    # -------------------------------------------------------------
    # 3. SQL INJECTION DEFENSE & SANITIZATION TESTS
    # -------------------------------------------------------------
    def test_sql_injection_resilience(self):
        """Verifies that query_transactions is immune to SQL injection attempts."""
        txs = [
            Transaccion(fecha="2024-01-10", concepto="Compra Segura", importe=200.0, descripcion="Desc", tipo=TipoTransaccion.EGRESO),
            Transaccion(fecha="2024-01-15", concepto="Cobro Normal", importe=500.0, descripcion="Desc", tipo=TipoTransaccion.INGRESO),
        ]
        save_processed_transactions("safe.pdf", compute_file_hash(b"safe"), "pdf", txs, db_path=self.db_path)

        # Injection payload 1: Classic drop table
        df_inj1 = query_transactions(busqueda="'; DROP TABLE transacciones; --", db_path=self.db_path)
        self.assertTrue(df_inj1.empty)

        # Confirm table still exists and data is intact
        df_check = query_transactions(db_path=self.db_path)
        self.assertEqual(len(df_check), 2)

        # Injection payload 2: OR 1=1 bypass
        df_inj2 = query_transactions(busqueda="' OR '1'='1", db_path=self.db_path)
        self.assertTrue(df_inj2.empty)

        # Injection payload 3: Wildcards in search
        df_wild = query_transactions(busqueda="%", db_path=self.db_path)
        # Should execute safely without error
        self.assertIsInstance(df_wild, pd.DataFrame)

    # -------------------------------------------------------------
    # 4. SPANISH BANKING DATE NORMALIZATION MATRIX
    # -------------------------------------------------------------
    def test_comprehensive_spanish_date_matrix(self):
        """Verifies date normalization across all 12 Spanish months and formatting variations."""
        date_cases = [
            # Short abbreviations
            ("01 ENE 24", "2024-01-01"),
            ("14 FEB 2024", "2024-02-14"),
            ("15 MAR 24", "2024-03-15"),
            ("30 ABR 2025", "2025-04-30"),
            ("05 MAY 24", "2024-05-05"),
            ("20 JUN 24", "2024-06-20"),
            ("01 JUL 2024", "2024-07-01"),
            ("15 AGO 24", "2024-08-15"),
            ("16 SEP 2024", "2024-09-16"),
            ("12 OCT 24", "2024-10-12"),
            ("20 NOV 2024", "2024-11-20"),
            ("25 DIC 24", "2024-12-25"),
            # Full month names
            ("01 DE ENERO DE 2024", "2024-01-01"),
            ("28 DE FEBRERO DEL 2024", "2024-02-28"),
            ("10 de marzo de 2025", "2025-03-10"),
            ("01 de mayo de 2024", "2024-05-01"),
            ("15 de septiembre de 2024", "2024-09-15"),
            ("31 de diciembre de 2023", "2023-12-31"),
            # Slash and dash separated
            ("15/09/2024", "2024-09-15"),
            ("15-09-2024", "2024-09-15"),
            ("15/SEP/2024", "2024-09-15"),
            ("15-sep-24", "2024-09-15"),
            # ISO timestamp
            ("2024-10-06T15:45:00", "2024-10-06"),
            ("2024-10-06", "2024-10-06"),
        ]

        for raw_date, expected in date_cases:
            normalized = normalize_spanish_date(raw_date)
            self.assertEqual(
                normalized,
                expected,
                f"Date normalization failed for: '{raw_date}'"
            )

        # Non-date or empty strings should safely produce valid ISO strings without throwing exceptions
        res_empty = normalize_spanish_date("")
        self.assertRegex(res_empty, r"^\d{4}-\d{2}-\d{2}$")
        res_invalid = normalize_spanish_date("fecha no valida")
        self.assertIsInstance(res_invalid, str)

    # -------------------------------------------------------------
    # 5. EXPORT DATA VALIDATION (EXCEL, CSV, TSV)
    # -------------------------------------------------------------
    def test_export_data_integrity(self):
        """Validates that export formats (Excel, CSV, TSV) adhere strictly to financial standards."""
        tx1 = Transaccion(fecha="2024-06-01", concepto="Honorarios SAT", importe=12500.50, descripcion="Factura F-101", tipo=TipoTransaccion.INGRESO)
        tx2 = Transaccion(fecha="2024-06-05", concepto="Papelería y Útiles", importe=345.80, descripcion="Compra con tarjeta", tipo=TipoTransaccion.EGRESO)

        df = create_standard_dataframe([tx1, tx2])
        df_ing = df[df["tipo"] == "ingreso"]
        df_eg = df[df["tipo"] == "egreso"]

        # 1. Excel export validation (.xlsx openpyxl)
        excel_bytes = df_to_excel_bytes(df_ing, df_eg)
        self.assertTrue(excel_bytes.startswith(b"PK"), "Excel file must start with ZIP PK header")

        xls = pd.ExcelFile(io.BytesIO(excel_bytes))
        self.assertEqual(xls.sheet_names, ["Ingresos", "Egresos"])

        df_in_read = pd.read_excel(xls, sheet_name="Ingresos")
        self.assertEqual(len(df_in_read), 1)
        self.assertEqual(df_in_read.iloc[0]["concepto"], "Honorarios SAT")
        self.assertEqual(df_in_read.iloc[0]["importe"], 12500.50)
        self.assertNotIn("tipo", df_in_read.columns, "Excel individual sheets must not include redundant 'tipo' column")

        # 2. CSV export validation (UTF-8 with BOM)
        csv_bytes = df_to_csv_bytes(df)
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"), "CSV must include UTF-8 BOM for Spanish Excel compatibility")
        csv_text = csv_bytes.decode("utf-8-sig")
        self.assertIn("Papelería y Útiles", csv_text, "CSV must preserve Spanish accents and special characters")
        self.assertIn("descripción", csv_text)

        # 3. TSV export validation (Tab-separated without index)
        tsv_str = df_to_tsv(df)
        lines = [line.rstrip("\r\n") for line in tsv_str.strip().split("\n")]
        self.assertGreaterEqual(len(lines), 3)
        self.assertEqual(lines[0].split("\t"), ["fecha", "concepto", "importe", "descripción"])
        self.assertIn("Honorarios SAT\t12500.5\tFactura F-101", lines[1])

    # -------------------------------------------------------------
    # 6. ETAPA 1: ACCOUNTING AMOUNT SANITIZATION & RECONCILIATION
    # -------------------------------------------------------------
    def test_accounting_amount_sanitization_parentheses_and_suffixes(self):
        """
        Verifies that sanitize_importe properly processes accounting formats:
        - Parentheses: (1,234.56), ($1,234.56)
        - Suffix minus: 1,234.56-, $1,234.56-
        - Preserves negative sign when keep_sign=True for Cargo/Abono validation
        - Yields absolute float when keep_sign=False or used in Transaccion.importe
        """
        accounting_cases = [
            ("(1,234.56)", -1234.56, 1234.56),
            ("1,234.56-", -1234.56, 1234.56),
            ("($1,234.56)", -1234.56, 1234.56),
            ("$1,234.56-", -1234.56, 1234.56),
            (" (1,234.56 MXN) ", -1234.56, 1234.56),
            ("-$500.00", -500.00, 500.00),
            ("$ 2,500.00 USD", 2500.00, 2500.00),
        ]

        for raw_val, expected_signed, expected_abs in accounting_cases:
            # Standalone function with sign preservation
            val_signed = sanitize_importe(raw_val, keep_sign=True)
            self.assertAlmostEqual(
                val_signed,
                expected_signed,
                places=2,
                msg=f"Failed keep_sign=True for: {raw_val}"
            )

            # Standalone function with absolute value
            val_abs = sanitize_importe(raw_val, keep_sign=False)
            self.assertAlmostEqual(
                val_abs,
                expected_abs,
                places=2,
                msg=f"Failed keep_sign=False for: {raw_val}"
            )

            # Pydantic Transaccion model must store absolute positive float
            tx = Transaccion(
                fecha="2024-03-01",
                concepto="Accounting Test",
                importe=raw_val,
                descripcion="Prueba contable",
                tipo=TipoTransaccion.EGRESO
            )
            self.assertAlmostEqual(
                tx.importe,
                expected_abs,
                places=2,
                msg=f"Transaccion.importe should be positive absolute float for: {raw_val}"
            )

    def test_reconciliacion_resultado_certainty_and_calculation(self):
        """
        Verifies ReconciliacionResultado attributes and mathematical validation:
        saldo_final_calculado = round(saldo_inicial + total_abonos - total_cargos, 2)
        diferencia = round(abs(saldo_final_esperado - saldo_final_calculado), 2)
        cuadra = diferencia <= 0.01
        """
        # Exact match
        res_exact = ReconciliacionResultado.validar(
            saldo_inicial=1000.0,
            total_abonos=500.0,
            total_cargos=200.0,
            saldo_final_esperado=1300.0
        )
        self.assertEqual(res_exact.saldo_inicial, 1000.0)
        self.assertEqual(res_exact.total_abonos, 500.0)
        self.assertEqual(res_exact.total_cargos, 200.0)
        self.assertEqual(res_exact.saldo_final_esperado, 1300.0)
        self.assertEqual(res_exact.saldo_final_calculado, 1300.0)
        self.assertEqual(res_exact.diferencia, 0.0)
        self.assertTrue(res_exact.cuadra)
        # Backward compatibility checks
        self.assertTrue(res_exact.conciliado)
        self.assertEqual(res_exact.total_ingresos, 500.0)
        self.assertEqual(res_exact.total_egresos, 200.0)

        # Tolerated rounding difference <= 0.01
        res_tol = ReconciliacionResultado.validar(
            saldo_inicial=1000.0,
            total_abonos=500.0,
            total_cargos=200.0,
            saldo_final_esperado=1300.01
        )
        self.assertEqual(res_tol.diferencia, 0.01)
        self.assertTrue(res_tol.cuadra)

        # Discrepancy > 0.01
        res_disc = ReconciliacionResultado.validar(
            saldo_inicial=1000.0,
            total_abonos=500.0,
            total_cargos=200.0,
            saldo_final_esperado=1295.50
        )
        self.assertEqual(res_disc.diferencia, 4.50)
        self.assertFalse(res_disc.cuadra)
        self.assertFalse(res_disc.conciliado)

        # Convenience helper
        res_helper = validar_reconciliacion(
            saldo_inicial=2500.0,
            total_abonos=1000.0,
            total_cargos=500.0,
            saldo_final_esperado=3000.0
        )
        self.assertEqual(res_helper.saldo_final_calculado, 3000.0)
        self.assertTrue(res_helper.cuadra)

    def test_spanish_dates_without_year_and_period_transition(self):
        """
        Verifies Spanish banking dates without explicit year (e.g. 15/ENE, 03-FEB, 12 MAR)
        infer year from period and properly handle December-to-January transitions.
        """
        # Inference with default_year
        self.assertEqual(normalize_spanish_date("15/ENE", default_year=2024), "2024-01-15")
        self.assertEqual(normalize_spanish_date("03-FEB", default_year=2024), "2024-02-03")
        self.assertEqual(normalize_spanish_date("12 MAR", default_year=2024), "2024-03-12")
        self.assertEqual(normalize_spanish_date("15/01", default_year=2024), "2024-01-15")

        # Cross-year statement period: 16 Dic 25 - 15 Ene 26
        period_str = "16 Dic 25 - 15 Ene 26"
        self.assertEqual(normalize_spanish_date("20/DIC", statement_period=period_str), "2025-12-20")
        self.assertEqual(normalize_spanish_date("05/ENE", statement_period=period_str), "2026-01-05")

        # Cross-year statement period with tuple of ISO dates
        period_tuple = ("2023-12-01", "2024-01-31")
        self.assertEqual(normalize_spanish_date("31/DIC", statement_period=period_tuple), "2023-12-31")
        self.assertEqual(normalize_spanish_date("02/ENE", statement_period=period_tuple), "2024-01-02")


if __name__ == "__main__":
    unittest.main()

