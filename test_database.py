"""
test_database.py - Automated QA Test Suite for SQLite Persistence Layer

Validates:
1. Database schema initialization, table structures, and B-Tree indexes
2. SHA-256 cryptographic duplicate detection
3. Atomic batch saving of transactions with date range extraction
4. Overwrite behavior without duplicate records
5. Foreign key enforcement and ON DELETE CASCADE
6. Multi-parameter filtered queries (dates, keyword search, type, account)
7. Aggregated database statistics
"""

import os
import gc
import tempfile
import unittest
import pandas as pd

from models import Transaccion, TipoTransaccion
from database import (
    db_session,
    init_db,
    compute_file_hash,
    check_file_processed,
    save_processed_transactions,
    query_transactions,
    get_distinct_files,
    delete_processed_file,
    get_database_stats,
    get_document_file_path,
    get_document_file_bytes,
    save_batch_checkpoint,
    get_batch_checkpoint,
    mark_checkpoint_file_completed,
    mark_checkpoint_interrupted,
    clear_batch_checkpoint,
)


class TestDatabaseLayer(unittest.TestCase):

    def setUp(self):
        """Creates an isolated temporary database for each test run."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_finanzas.db")
        init_db(self.db_path)

    def tearDown(self):
        """Cleans up temporary directory after each test."""
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_schema_and_indexes_initialization(self):
        """Verifies that all required tables and indexes are created properly."""
        with db_session(self.db_path) as conn:
            # Check tables
            tables = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                ).fetchall()
            ]
            self.assertIn("archivos_procesados", tables)
            self.assertIn("transacciones", tables)

            # Check indexes
            indexes = [
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index';"
                ).fetchall()
            ]
            self.assertIn("idx_transacciones_fecha", indexes)
            self.assertIn("idx_transacciones_concepto", indexes)
            self.assertIn("idx_transacciones_tipo", indexes)
            self.assertIn("idx_transacciones_archivo", indexes)
            self.assertIn("idx_archivos_nombre", indexes)
            self.assertIn("idx_archivos_hash", indexes)

    def test_compute_file_hash(self):
        """Verifies deterministic SHA-256 hash generation."""
        content1 = b"PDF-STATEMENT-SAMPLE-A"
        content2 = b"PDF-STATEMENT-SAMPLE-B"
        hash1 = compute_file_hash(content1)
        hash2 = compute_file_hash(content2)

        self.assertEqual(len(hash1), 64)
        self.assertEqual(hash1, compute_file_hash(content1))
        self.assertNotEqual(hash1, hash2)

    def test_save_and_duplicate_detection(self):
        """Verifies saving transactions and retrieving duplicate detection metadata."""
        sample_bytes = b"Sample Bank Statement Content 2024"
        file_hash = compute_file_hash(sample_bytes)
        fname = "estado_cuenta_marzo.pdf"

        # Initially, file should not exist
        initial_check = check_file_processed(fname, file_hash, db_path=self.db_path)
        self.assertIsNone(initial_check)

        # Create mock transactions
        txs = [
            Transaccion(
                fecha="2024-03-05",
                concepto="Nomina Empresa",
                importe=25000.0,
                descripcion="Transferencia SPEI recibida",
                tipo=TipoTransaccion.INGRESO
            ),
            Transaccion(
                fecha="2024-03-12",
                concepto="Supermercado Chedraui",
                importe=1450.50,
                descripcion="Compra tarjeta débito",
                tipo=TipoTransaccion.EGRESO
            ),
            Transaccion(
                fecha="2024-03-25",
                concepto="Pago Servicio CFE",
                importe=620.00,
                descripcion="Pago en línea luz",
                tipo=TipoTransaccion.EGRESO
            ),
        ]

        archivo_id = save_processed_transactions(
            filename=fname,
            file_hash=file_hash,
            file_type="pdf",
            transactions=txs,
            overwrite=False,
            db_path=self.db_path
        )
        self.assertGreater(archivo_id, 0)

        # Check duplicate by hash and filename
        duplicate_check = check_file_processed(fname, file_hash, db_path=self.db_path)
        self.assertIsNotNone(duplicate_check)
        self.assertEqual(duplicate_check["nombre_archivo"], fname)
        self.assertEqual(duplicate_check["total_transacciones"], 3)
        self.assertEqual(duplicate_check["rango_fecha_inicio"], "2024-03-05")
        self.assertEqual(duplicate_check["rango_fecha_fin"], "2024-03-25")

    def test_overwrite_existing_file(self):
        """Verifies that overwrite=True removes old records and updates transactions without duplication."""
        sample_bytes = b"Statement Version 1"
        file_hash = compute_file_hash(sample_bytes)
        fname = "banamex_ene.pdf"

        txs_v1 = [
            Transaccion(
                fecha="2024-01-10",
                concepto="Gasolina BP",
                importe=500.0,
                descripcion="Carga combustible",
                tipo=TipoTransaccion.EGRESO
            )
        ]
        save_processed_transactions(fname, file_hash, "pdf", txs_v1, overwrite=False, db_path=self.db_path)

        stats_before = get_database_stats(self.db_path)
        self.assertEqual(stats_before["total_archivos"], 1)
        self.assertEqual(stats_before["total_transacciones"], 1)

        # Now re-process with 2 transactions and overwrite=True
        txs_v2 = [
            Transaccion(
                fecha="2024-01-10",
                concepto="Gasolina BP",
                importe=500.0,
                descripcion="Carga combustible",
                tipo=TipoTransaccion.EGRESO
            ),
            Transaccion(
                fecha="2024-01-20",
                concepto="Restaurante La Parrilla",
                importe=850.0,
                descripcion="Consumo alimentos",
                tipo=TipoTransaccion.EGRESO
            )
        ]
        save_processed_transactions(fname, file_hash, "pdf", txs_v2, overwrite=True, db_path=self.db_path)

        stats_after = get_database_stats(self.db_path)
        self.assertEqual(stats_after["total_archivos"], 1)
        self.assertEqual(stats_after["total_transacciones"], 2)

    def test_generic_filename_collision_no_data_loss(self):
        """Verifies that two bank statements with identical filenames but different hashes coexist safely."""
        fname = "EstadoDeCuenta.pdf"
        hash_jan = compute_file_hash(b"January Statement Bytes")
        hash_feb = compute_file_hash(b"February Statement Bytes")

        txs_jan = [
            Transaccion(
                fecha="2024-01-15",
                concepto="Nomina Enero",
                importe=15000.0,
                descripcion="Pago quincenal",
                tipo=TipoTransaccion.INGRESO
            )
        ]
        txs_feb = [
            Transaccion(
                fecha="2024-02-15",
                concepto="Nomina Febrero",
                importe=15000.0,
                descripcion="Pago quincenal",
                tipo=TipoTransaccion.INGRESO
            )
        ]

        save_processed_transactions(fname, hash_jan, "pdf", txs_jan, overwrite=False, db_path=self.db_path)
        # Upload February with the exact same generic filename and overwrite=True
        save_processed_transactions(fname, hash_feb, "pdf", txs_feb, overwrite=True, db_path=self.db_path)

        # Both records must be preserved
        stats = get_database_stats(self.db_path)
        self.assertEqual(stats["total_archivos"], 2)
        self.assertEqual(stats["total_transacciones"], 2)

        # Query each by hash exclusively
        check_jan = check_file_processed(fname, hash_jan, db_path=self.db_path)
        check_feb = check_file_processed(fname, hash_feb, db_path=self.db_path)
        self.assertIsNotNone(check_jan)
        self.assertIsNotNone(check_feb)
        self.assertNotEqual(check_jan["id"], check_feb["id"])


    def test_cascade_deletion(self):
        """Verifies that deleting an archivos_procesados record automatically deletes its transacciones."""
        fname = "bbva_feb.pdf"
        file_hash = compute_file_hash(b"bbva content")
        txs = [
            Transaccion(
                fecha="2024-02-01",
                concepto="Intereses Ganados",
                importe=120.45,
                descripcion="Rendimiento cuenta",
                tipo=TipoTransaccion.INGRESO
            ),
            Transaccion(
                fecha="2024-02-15",
                concepto="Comisión Manejo Cuenta",
                importe=70.0,
                descripcion="Comisión mensual",
                tipo=TipoTransaccion.EGRESO
            ),
        ]
        archivo_id = save_processed_transactions(fname, file_hash, "pdf", txs, overwrite=False, db_path=self.db_path)

        # Verify insertion
        files_list = get_distinct_files(self.db_path)
        self.assertEqual(len(files_list), 1)

        df_txs = query_transactions(db_path=self.db_path)
        self.assertEqual(len(df_txs), 2)

        # Delete statement
        deleted = delete_processed_file(archivo_id, db_path=self.db_path)
        self.assertTrue(deleted)

        # Verify cascade
        self.assertEqual(len(get_distinct_files(self.db_path)), 0)
        df_remaining = query_transactions(db_path=self.db_path)
        self.assertTrue(df_remaining.empty)

    def test_query_filtering(self):
        """Tests combined date, keyword, and type filters."""
        fname = "consolidado_2024.pdf"
        file_hash = compute_file_hash(b"consolidado content")
        txs = [
            Transaccion(
                fecha="2024-01-15",
                concepto="Amazon MX Compra",
                importe=1200.0,
                descripcion="Artículos de oficina folio 99281",
                tipo=TipoTransaccion.EGRESO
            ),
            Transaccion(
                fecha="2024-02-10",
                concepto="Cliente ABC Pago Factura",
                importe=35000.0,
                descripcion="Cobro honorarios profesionales",
                tipo=TipoTransaccion.INGRESO
            ),
            Transaccion(
                fecha="2024-03-01",
                concepto="Amazon Web Services",
                importe=450.0,
                descripcion="Servidores nube hosting",
                tipo=TipoTransaccion.EGRESO
            ),
        ]
        archivo_id = save_processed_transactions(fname, file_hash, "pdf", txs, overwrite=False, db_path=self.db_path)

        # 1. Date range filter
        df_dates = query_transactions(fecha_desde="2024-02-01", fecha_hasta="2024-03-31", db_path=self.db_path)
        self.assertEqual(len(df_dates), 2)

        # 2. Case-insensitive keyword filter
        df_search = query_transactions(busqueda="amazon", db_path=self.db_path)
        self.assertEqual(len(df_search), 2)
        self.assertTrue(all("amazon" in c.lower() for c in df_search["concepto"]))

        # 3. Type filter
        df_ingresos = query_transactions(tipo="ingreso", db_path=self.db_path)
        self.assertEqual(len(df_ingresos), 1)
        self.assertEqual(df_ingresos.iloc[0]["concepto"], "Cliente ABC Pago Factura")

        # 4. File ID filter
        df_file = query_transactions(archivo_id=archivo_id, db_path=self.db_path)
        self.assertEqual(len(df_file), 3)

        # 5. Non-matching filter returns empty DataFrame with expected columns
        df_empty = query_transactions(busqueda="inexistente", db_path=self.db_path)
        self.assertTrue(df_empty.empty)
        self.assertIn("descripción", df_empty.columns)
        self.assertIn("importe", df_empty.columns)

    def test_database_stats(self):
        """Verifies accurate calculation of database global metrics."""
        fname = "movimientos.xml"
        file_hash = compute_file_hash(b"xml content")
        txs = [
            Transaccion(
                fecha="2024-04-01",
                concepto="Depósito Efectivo",
                importe=5000.0,
                descripcion="Abono en ventanilla",
                tipo=TipoTransaccion.INGRESO
            ),
            Transaccion(
                fecha="2024-04-15",
                concepto="Renta Oficina",
                importe=3200.0,
                descripcion="Pago mensual local",
                tipo=TipoTransaccion.EGRESO
            ),
        ]
        save_processed_transactions(fname, file_hash, "xml", txs, overwrite=False, db_path=self.db_path)

        stats = get_database_stats(self.db_path)
        self.assertEqual(stats["total_archivos"], 1)
        self.assertEqual(stats["total_transacciones"], 2)
        self.assertAlmostEqual(stats["total_ingresos"], 5000.0)
        self.assertAlmostEqual(stats["total_egresos"], 3200.0)
        self.assertAlmostEqual(stats["balance_neto"], 1800.0)
        self.assertEqual(stats["fecha_min"], "2024-04-01")
        self.assertEqual(stats["fecha_max"], "2024-04-15")

    def test_document_file_persistence_and_retrieval(self):
        """Verifies that statement file bytes are persistently stored in documents dir and retrievable."""
        fname = "estado_cuenta_audit.pdf"
        sample_bytes = b"%PDF-1.4 Mock Binary Content for Bank Statement"
        file_hash = compute_file_hash(sample_bytes)
        txs = [
            Transaccion(
                fecha="2024-05-10",
                concepto="Transferencia Interbancaria",
                importe=12500.0,
                descripcion="SPEI Recibido",
                tipo=TipoTransaccion.INGRESO
            )
        ]

        # Save with file_bytes
        archivo_id = save_processed_transactions(
            filename=fname,
            file_hash=file_hash,
            file_type="pdf",
            transactions=txs,
            file_bytes=sample_bytes,
            overwrite=False,
            db_path=self.db_path
        )

        # 1. Path retrieval
        stored_path = get_document_file_path(archivo_id, db_path=self.db_path)
        self.assertIsNotNone(stored_path)
        self.assertTrue(os.path.exists(stored_path))
        self.assertTrue(stored_path.endswith(f"{file_hash[:16]}_{fname}"))

        # 2. Binary content retrieval
        res = get_document_file_bytes(archivo_id, db_path=self.db_path)
        self.assertIsNotNone(res)
        retrieved_name, retrieved_bytes = res
        self.assertEqual(retrieved_name, fname)
        self.assertEqual(retrieved_bytes, sample_bytes)

        # 3. Verify get_distinct_files metadata
        files = get_distinct_files(db_path=self.db_path)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["ruta_archivo"], stored_path)
        self.assertEqual(files[0]["tamano_bytes"], len(sample_bytes))

    def test_document_file_cascade_deletion(self):
        """Verifies that delete_processed_file also deletes the physical document on disk."""
        fname = "to_delete.xml"
        raw_bytes = b"<xml><movimiento>Test</movimiento></xml>"
        fhash = compute_file_hash(raw_bytes)
        txs = [
            Transaccion(
                fecha="2024-06-01",
                concepto="Comisión Cuenta",
                importe=200.0,
                descripcion="Cargo mensual",
                tipo=TipoTransaccion.EGRESO
            )
        ]

        archivo_id = save_processed_transactions(
            filename=fname,
            file_hash=fhash,
            file_type="xml",
            transactions=txs,
            file_bytes=raw_bytes,
            overwrite=False,
            db_path=self.db_path
        )
        stored_path = get_document_file_path(archivo_id, db_path=self.db_path)
        self.assertTrue(os.path.exists(stored_path))

        # Delete statement
        deleted = delete_processed_file(archivo_id, db_path=self.db_path)
        self.assertTrue(deleted)

        # Verify physical file was removed from disk
        self.assertFalse(os.path.exists(stored_path))

    def test_document_file_overwrite(self):
        """Verifies that overwriting with new content cleans up the old physical file."""
        fname = "versioned_statement.pdf"
        v1_bytes = b"PDF Version 1 Content"
        v2_bytes = b"PDF Version 2 Content Updated"
        h1 = compute_file_hash(v1_bytes)
        h2 = compute_file_hash(v2_bytes)
        txs = [
            Transaccion(fecha="2024-07-01", concepto="V1", importe=100.0, descripcion="V1 Desc", tipo=TipoTransaccion.INGRESO)
        ]

        # Save V1
        id_1 = save_processed_transactions(
            filename=fname,
            file_hash=h1,
            file_type="pdf",
            transactions=txs,
            file_bytes=v1_bytes,
            overwrite=False,
            db_path=self.db_path
        )
        path_v1 = get_document_file_path(id_1, db_path=self.db_path)
        self.assertTrue(os.path.exists(path_v1))

        # Overwrite with V2 (updated statement file with renamed target)
        id_2 = save_processed_transactions(
            filename="reemplazo_renombrado.pdf",
            file_hash=h1,
            file_type="pdf",
            transactions=txs,
            file_bytes=v2_bytes,
            overwrite=True,
            db_path=self.db_path
        )
        path_v2 = get_document_file_path(id_2, db_path=self.db_path)
        self.assertTrue(os.path.exists(path_v2))
        self.assertFalse(os.path.exists(path_v1))


    def test_batch_checkpoint_lifecycle(self):
        """Verifies checkpoint creation, file completion updates, and successful clearing."""
        batch_id = "test_batch_001"
        files = ["doc1.pdf", "doc2.xml", "doc3.pdf"]

        # Initial checkpoint
        save_batch_checkpoint(
            batch_id=batch_id,
            total_files=len(files),
            completed_files=[],
            status="in_progress",
            db_path=self.db_path
        )

        cp = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["batch_id"], batch_id)
        self.assertEqual(cp["estado"], "in_progress")
        self.assertEqual(cp["archivos_totales"], 3)
        self.assertEqual(len(cp["archivos_completados"]), 0)

        # Mark doc1.pdf completed
        mark_checkpoint_file_completed(batch_id, "doc1.pdf", db_path=self.db_path)
        cp = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertIn("doc1.pdf", cp["archivos_completados"])
        self.assertEqual(len(cp["archivos_completados"]), 1)

        # Mark doc2.xml completed
        mark_checkpoint_file_completed(batch_id, "doc2.xml", db_path=self.db_path)
        cp = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertEqual(len(cp["archivos_completados"]), 2)

        # Clear checkpoint
        clear_batch_checkpoint(batch_id, db_path=self.db_path)
        cp_cleared = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertIsNone(cp_cleared)

    def test_batch_checkpoint_interruption_and_recovery(self):
        """Verifies recording interruption on failure and resuming from where it stopped."""
        batch_id = "test_batch_err"
        files = ["estado_1.pdf", "estado_2_broken.xml", "estado_3.pdf"]

        # Progress to file 1
        save_batch_checkpoint(
            batch_id=batch_id,
            total_files=3,
            completed_files=["estado_1.pdf"],
            status="in_progress",
            db_path=self.db_path
        )

        # File 2 encounters an error
        err_message = "Ollama connection timeout on port 11434"
        mark_checkpoint_interrupted(
            batch_id=batch_id,
            failed_file="estado_2_broken.xml",
            error_message=err_message,
            db_path=self.db_path
        )

        cp = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertIsNotNone(cp)
        self.assertEqual(cp["estado"], "interrupted")
        self.assertEqual(cp["archivo_fallido"], "estado_2_broken.xml")
        self.assertEqual(cp["ultimo_error"], err_message)
        self.assertIn("estado_1.pdf", cp["archivos_completados"])

        # Global lookup finds the interrupted checkpoint
        latest_cp = get_batch_checkpoint(None, db_path=self.db_path)
        self.assertIsNotNone(latest_cp)
        self.assertEqual(latest_cp["batch_id"], batch_id)

        # Resumption: simulate resolving/retrying file 2 and file 3
        mark_checkpoint_file_completed(batch_id, "estado_2_broken.xml", db_path=self.db_path)
        mark_checkpoint_file_completed(batch_id, "estado_3.pdf", db_path=self.db_path)

        cp_done = get_batch_checkpoint(batch_id, db_path=self.db_path)
        self.assertEqual(cp_done["estado"], "completed")
        self.assertEqual(len(cp_done["archivos_completados"]), 3)


if __name__ == "__main__":
    unittest.main()

