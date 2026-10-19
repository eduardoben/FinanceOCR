"""
database.py - Local Data Access Layer (DAL) for Financial Transactions

Provides persistent, 100% local SQLite storage for bank statements and extracted
transactions with:
1. Cryptographic SHA-256 duplicate detection
2. Relational integrity with ON DELETE CASCADE
3. Fast B-Tree indexing for date, concept, and account queries
4. High-performance WAL (Write-Ahead Logging) mode for concurrent Streamlit sessions
5. Safe session lifecycle with contextlib to prevent file locks on Windows
"""

import os
import shutil
import sqlite3
import hashlib
import json
from datetime import datetime
from contextlib import contextmanager
from typing import List, Optional, Dict, Any, Generator
import pandas as pd

from models import Transaccion, TipoTransaccion, ReconciliacionResultado

# Canonical storage directory and database location
DEFAULT_DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "finanzas.db")
DEFAULT_DOCS_DIR = os.path.join(DEFAULT_DB_DIR, "documentos")


def get_docs_dir(db_path: str = DEFAULT_DB_PATH) -> str:
    """
    Returns the documents directory corresponding to the database directory.
    Creates the directory if it does not exist.
    """
    base_dir = os.path.dirname(os.path.abspath(db_path))
    docs_dir = os.path.join(base_dir, "documentos")
    os.makedirs(docs_dir, exist_ok=True)
    return docs_dir


def get_db_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Establishes a connection to the SQLite database with WAL mode and foreign keys enabled.

    Args:
        db_path: Absolute or relative file path to the SQLite database.

    Returns:
        sqlite3.Connection: Configured database connection with row factory.
    """
    db_dir = os.path.dirname(os.path.abspath(db_path))
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def db_session(db_path: str = DEFAULT_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager that handles transaction commit/rollback and ensures
    explicit connection closure, preventing open file descriptor locks.

    Args:
        db_path: Target database file path.

    Yields:
        sqlite3.Connection: Active database connection.
    """
    conn = get_db_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# In-memory registry to eliminate redundant DDL execution on warm databases
_INITIALIZED_DBS: set[str] = set()


def hash_sha256(file_bytes: bytes) -> str:
    """
    Calcula el hash criptográfico SHA-256 sobre el contenido binario del archivo
    para identificar de forma única cada estado de cuenta y evitar colisiones de nombre.

    Args:
        file_bytes: Contenido binario del archivo subido (PDF o XML).

    Returns:
        str: Digest hexadecimal en minúsculas de 64 caracteres.
    """
    return hashlib.sha256(file_bytes).hexdigest()


def compute_file_hash(file_bytes: bytes) -> str:
    """
    Alias retrocompatible para hash_sha256.
    """
    return hash_sha256(file_bytes)


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Initializes database schema and B-Tree indexes if they do not already exist.
    Also executes safe migrations for new columns (ruta_archivo, tamano_bytes, reconciliation fields).

    Args:
        db_path: Target database file path.
    """
    abs_p = os.path.normcase(os.path.abspath(db_path))
    if abs_p in _INITIALIZED_DBS and os.path.exists(abs_p):
        return

    ddl_script = """
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS archivos_procesados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_archivo TEXT NOT NULL,
        hash_sha256 TEXT NOT NULL UNIQUE,
        tipo_archivo TEXT NOT NULL,
        fecha_procesamiento TEXT NOT NULL,
        total_transacciones INTEGER NOT NULL DEFAULT 0,
        rango_fecha_inicio TEXT,
        rango_fecha_fin TEXT,
        ruta_archivo TEXT,
        tamano_bytes INTEGER,
        saldo_inicial REAL,
        saldo_final REAL,
        diferencia_conciliacion REAL,
        conciliado INTEGER,
        estado_conciliacion TEXT,
        modo_contable TEXT,
        cobertura REAL,
        metricas_json TEXT
    );

    CREATE TABLE IF NOT EXISTS transacciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        archivo_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        concepto TEXT NOT NULL,
        importe REAL NOT NULL,
        descripcion TEXT,
        tipo TEXT NOT NULL CHECK(tipo IN ('ingreso', 'egreso')),
        confianza REAL DEFAULT 1.0,
        origen TEXT DEFAULT 'llm',
        linea_origen TEXT DEFAULT '',
        saldo_renglon REAL,
        estado_revision TEXT DEFAULT 'pendiente',
        pagina INTEGER DEFAULT 1,
        FOREIGN KEY (archivo_id) REFERENCES archivos_procesados(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS filas_rechazadas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        archivo_id INTEGER NOT NULL,
        linea_origen TEXT,
        datos_crudos TEXT,
        motivo TEXT,
        detalle_error TEXT,
        pagina INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (archivo_id) REFERENCES archivos_procesados(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_transacciones_fecha ON transacciones(fecha);
    CREATE INDEX IF NOT EXISTS idx_transacciones_concepto ON transacciones(concepto);
    CREATE INDEX IF NOT EXISTS idx_transacciones_tipo ON transacciones(tipo);
    CREATE INDEX IF NOT EXISTS idx_transacciones_archivo ON transacciones(archivo_id);
    CREATE INDEX IF NOT EXISTS idx_filas_rechazadas_archivo ON filas_rechazadas(archivo_id);
    CREATE INDEX IF NOT EXISTS idx_archivos_nombre ON archivos_procesados(nombre_archivo);
    CREATE INDEX IF NOT EXISTS idx_archivos_hash ON archivos_procesados(hash_sha256);

    CREATE TABLE IF NOT EXISTS lotes_checkpoints (
        batch_id TEXT PRIMARY KEY,
        estado TEXT NOT NULL,
        archivos_totales INTEGER NOT NULL,
        archivos_completados TEXT NOT NULL,
        archivo_fallido TEXT,
        ultimo_error TEXT,
        configuraciones TEXT,
        fecha_actualizacion TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_checkpoints_estado ON lotes_checkpoints(estado);
    """
    with db_session(db_path) as conn:
        conn.executescript(ddl_script)
        # Automatic non-destructive schema migrations for existing databases
        for col, col_type in [
            ("ruta_archivo", "TEXT"),
            ("tamano_bytes", "INTEGER"),
            ("saldo_inicial", "REAL"),
            ("saldo_final", "REAL"),
            ("diferencia_conciliacion", "REAL"),
            ("conciliado", "INTEGER"),
            ("estado_conciliacion", "TEXT"),
            ("modo_contable", "TEXT"),
            ("cobertura", "REAL"),
            ("metricas_json", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE archivos_procesados ADD COLUMN {col} {col_type};")
            except sqlite3.OperationalError:
                pass

        for col, col_type in [
            ("confianza", "REAL DEFAULT 1.0"),
            ("origen", "TEXT DEFAULT 'llm'"),
            ("linea_origen", "TEXT DEFAULT ''"),
            ("saldo_renglon", "REAL"),
            ("estado_revision", "TEXT DEFAULT 'pendiente'"),
            ("pagina", "INTEGER DEFAULT 1"),
        ]:
            try:
                conn.execute(f"ALTER TABLE transacciones ADD COLUMN {col} {col_type};")
            except sqlite3.OperationalError:
                pass

        for col, col_type in [
            ("pagina", "INTEGER DEFAULT 1"),
        ]:
            try:
                conn.execute(f"ALTER TABLE filas_rechazadas ADD COLUMN {col} {col_type};")
            except sqlite3.OperationalError:
                pass

    # Recover any legacy statements stored in temp files into data/documentos
    try:
        sync_legacy_stored_files(db_path)
    except Exception:
        pass

    _INITIALIZED_DBS.add(abs_p)



def sync_legacy_stored_files(db_path: str = DEFAULT_DB_PATH) -> int:
    """
    Recovers any legacy bank statements previously processed whose physical copies
    exist in the system temp directory, copying them into the persistent data/documentos folder.
    """
    import tempfile
    import shutil
    docs_dir = get_docs_dir(db_path)
    temp_dir = tempfile.gettempdir()
    updated = 0
    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT id, nombre_archivo, hash_sha256 FROM archivos_procesados WHERE ruta_archivo IS NULL OR ruta_archivo = '';"
        ).fetchall()
        for r in rows:
            f_id = r["id"]
            fname = r["nombre_archivo"]
            fhash = r["hash_sha256"]
            safe_name = "".join(c for c in os.path.basename(fname) if c.isalnum() or c in "._- ")
            tmp_candidate = os.path.join(temp_dir, f"stm_{safe_name}")
            target_name = f"{fhash[:16]}_{safe_name}"
            target_path = os.path.join(docs_dir, target_name)

            if os.path.exists(target_path):
                size = os.path.getsize(target_path)
                conn.execute("UPDATE archivos_procesados SET ruta_archivo = ?, tamano_bytes = ? WHERE id = ?;", (target_path, size, f_id))
                updated += 1
            elif os.path.exists(tmp_candidate):
                try:
                    shutil.copy2(tmp_candidate, target_path)
                    size = os.path.getsize(target_path)
                    conn.execute("UPDATE archivos_procesados SET ruta_archivo = ?, tamano_bytes = ? WHERE id = ?;", (target_path, size, f_id))
                    updated += 1
                except Exception:
                    pass
    return updated


def save_document_file(
    filename: str,
    file_bytes: bytes,
    file_hash: str,
    db_path: str = DEFAULT_DB_PATH
) -> str:
    """
    Persists a copy of the uploaded bank statement in the dedicated documents folder.
    Prefixes with the first 16 characters of SHA-256 to ensure collision resistance.

    Args:
        filename: Original file name.
        file_bytes: Raw binary content of the file.
        file_hash: SHA-256 digest.
        db_path: Database path to locate corresponding documents directory.

    Returns:
        str: Absolute path of the stored file.
    """
    docs_dir = get_docs_dir(db_path)
    safe_name = "".join(c for c in os.path.basename(filename) if c.isalnum() or c in "._- ")
    target_name = f"{file_hash[:16]}_{safe_name}"
    target_path = os.path.join(docs_dir, target_name)
    with open(target_path, "wb") as f:
        f.write(file_bytes)
    return target_path


def get_document_file_path(
    archivo_id: int,
    db_path: str = DEFAULT_DB_PATH
) -> Optional[str]:
    """
    Retrieves the stored file path on disk for a given statement ID.

    Args:
        archivo_id: Primary key ID in archivos_procesados.
        db_path: Database path.

    Returns:
        Optional[str]: Path to the physical file if it exists, otherwise None.
    """
    init_db(db_path)
    query = "SELECT ruta_archivo FROM archivos_procesados WHERE id = ?;"
    with db_session(db_path) as conn:
        row = conn.execute(query, (archivo_id,)).fetchone()
        if row and row["ruta_archivo"] and os.path.exists(row["ruta_archivo"]):
            return row["ruta_archivo"]
    return None


def get_document_file_bytes(
    archivo_id: int,
    db_path: str = DEFAULT_DB_PATH
) -> Optional[tuple[str, bytes]]:
    """
    Reads the stored file binary content and original filename for a given statement ID.

    Args:
        archivo_id: Primary key ID in archivos_procesados.
        db_path: Database path.

    Returns:
        Optional[tuple[str, bytes]]: (filename, file_bytes) if found, otherwise None.
    """
    init_db(db_path)
    query = "SELECT nombre_archivo, ruta_archivo FROM archivos_procesados WHERE id = ?;"
    with db_session(db_path) as conn:
        row = conn.execute(query, (archivo_id,)).fetchone()
        if row and row["ruta_archivo"] and os.path.exists(row["ruta_archivo"]):
            with open(row["ruta_archivo"], "rb") as f:
                return row["nombre_archivo"], f.read()
    return None


# Note: hash_sha256 and compute_file_hash are defined above.


def check_file_processed(
    filename: str,
    file_hash: str,
    db_path: str = DEFAULT_DB_PATH
) -> Optional[Dict[str, Any]]:
    """
    Checks if a bank statement was previously processed by cryptographic hash.

    Args:
        filename: Name of the uploaded file (retained for signature compatibility).
        file_hash: SHA-256 hex digest of the file bytes.
        db_path: Target database path.

    Returns:
        Optional[Dict[str, Any]]: Metadata record if file exists, otherwise None.
    """
    init_db(db_path)
    query = """
    SELECT id, nombre_archivo, hash_sha256, tipo_archivo, fecha_procesamiento,
           total_transacciones, rango_fecha_inicio, rango_fecha_fin,
           ruta_archivo, tamano_bytes,
           saldo_inicial, saldo_final, diferencia_conciliacion, conciliado,
           estado_conciliacion, modo_contable, cobertura, metricas_json
    FROM archivos_procesados
    WHERE hash_sha256 = ?
    LIMIT 1;
    """
    with db_session(db_path) as conn:
        cursor = conn.execute(query, (file_hash,))
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def save_processed_transactions(
    filename: str,
    file_hash: str,
    file_type: str,
    transactions: List[Transaccion],
    file_bytes: Optional[bytes] = None,
    stored_path: Optional[str] = None,
    overwrite: bool = False,
    saldo_inicial: Optional[float] = None,
    saldo_final: Optional[float] = None,
    diferencia_conciliacion: Optional[float] = None,
    conciliado: Optional[bool] = None,
    estado_conciliacion: Optional[str] = None,
    modo_contable: Optional[str] = None,
    cobertura: Optional[float] = None,
    metricas_json: Optional[str] = None,
    filas_rechazadas: Optional[List[Dict[str, Any]]] = None,
    db_path: str = DEFAULT_DB_PATH
) -> int:
    """
    Persists an extracted bank statement, its transactions, and its source document atomically.

    If overwrite is True, any existing record matching the hash is deleted beforehand
    along with its transactions via ON DELETE CASCADE and old stored file.

    Args:
        filename: Name of the statement file.
        file_hash: SHA-256 digest of the file.
        file_type: 'pdf' or 'xml'.
        transactions: List of validated Transaccion models.
        file_bytes: Optional binary content of the statement to store permanently.
        stored_path: Optional existing path to the stored document.
        overwrite: When True, replaces prior occurrences of this statement.
        saldo_inicial: Optional opening balance stated in document.
        saldo_final: Optional closing balance stated in document.
        diferencia_conciliacion: Optional difference from arithmetic reconciliation.
        conciliado: Optional boolean flag indicating mathematical reconciliation.
        estado_conciliacion: Optional document classification ('VERIFICADO', 'REVISAR', 'RECHAZADO').
        modo_contable: Optional accounting mode ('debito', 'credito', 'ambiguo').
        cobertura: Optional capture coverage ratio (0.0 to 1.0).
        metricas_json: Optional serialized JSON string of extraction metrics.
        filas_rechazadas: Optional list of audited rejected rows to persist.
        db_path: Database path.

    Returns:
        int: The primary key ID of the created archivos_procesados record.
    """
    init_db(db_path)

    # Determine date range from transactions if available
    dates = [tx.fecha for tx in transactions if tx.fecha and tx.fecha.strip()]
    rango_inicio = min(dates) if dates else None
    rango_fin = max(dates) if dates else None
    fecha_proc = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Persist physical document to data/documentos/ if bytes provided
    ruta_almacenada = stored_path
    tamano = len(file_bytes) if file_bytes is not None else (
        os.path.getsize(stored_path) if stored_path and os.path.exists(stored_path) else None
    )

    if file_bytes is not None and not ruta_almacenada:
        ruta_almacenada = save_document_file(filename, file_bytes, file_hash, db_path=db_path)

    with db_session(db_path) as conn:
        if overwrite:
            # Query and delete prior physical file if it differs
            old_rows = conn.execute(
                "SELECT ruta_archivo FROM archivos_procesados WHERE hash_sha256 = ?;",
                (file_hash,)
            ).fetchall()
            for r in old_rows:
                old_p = r["ruta_archivo"]
                if old_p and os.path.exists(old_p) and old_p != ruta_almacenada:
                    try:
                        os.unlink(old_p)
                    except Exception:
                        pass

            # Delete prior record solely by hash (cascades to transacciones and filas_rechazadas)
            conn.execute(
                "DELETE FROM archivos_procesados WHERE hash_sha256 = ?;",
                (file_hash,)
            )

        # Insert parent file record
        conciliado_int = 1 if conciliado is True else (0 if conciliado is False else None)
        cursor = conn.execute(
            """
            INSERT INTO archivos_procesados (
                nombre_archivo, hash_sha256, tipo_archivo,
                fecha_procesamiento, total_transacciones,
                rango_fecha_inicio, rango_fecha_fin,
                ruta_archivo, tamano_bytes,
                saldo_inicial, saldo_final,
                diferencia_conciliacion, conciliado,
                estado_conciliacion, modo_contable,
                cobertura, metricas_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                filename,
                file_hash,
                file_type.lower(),
                fecha_proc,
                len(transactions),
                rango_inicio,
                rango_fin,
                ruta_almacenada,
                tamano,
                saldo_inicial,
                saldo_final,
                diferencia_conciliacion,
                conciliado_int,
                estado_conciliacion,
                modo_contable,
                cobertura,
                metricas_json
            )
        )
        archivo_id = cursor.lastrowid

        # Batch insert transactions with full traceability
        tx_rows = []
        for tx in transactions:
            tipo_val = tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
            tx_rows.append((
                archivo_id,
                tx.fecha,
                tx.concepto,
                float(tx.importe),
                tx.descripcion,
                tipo_val,
                float(getattr(tx, "confianza", 1.0)),
                str(getattr(tx, "origen", "llm")),
                str(getattr(tx, "linea_origen", "")),
                float(tx.saldo_renglon) if getattr(tx, "saldo_renglon", None) is not None else None,
                str(getattr(tx, "estado_revision", "pendiente")),
                int(getattr(tx, "pagina", 1))
            ))

        if tx_rows:
            for tx in transactions:
                tipo_val = tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
                cur = conn.execute(
                    """
                    INSERT INTO transacciones (
                        archivo_id, fecha, concepto, importe, descripcion, tipo,
                        confianza, origen, linea_origen, saldo_renglon, estado_revision, pagina
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        archivo_id,
                        tx.fecha,
                        tx.concepto,
                        float(tx.importe),
                        tx.descripcion,
                        tipo_val,
                        float(getattr(tx, "confianza", 1.0)),
                        str(getattr(tx, "origen", "llm")),
                        str(getattr(tx, "linea_origen", "")),
                        float(tx.saldo_renglon) if getattr(tx, "saldo_renglon", None) is not None else None,
                        str(getattr(tx, "estado_revision", "pendiente")),
                        int(getattr(tx, "pagina", 1))
                    )
                )
                tx.id = cur.lastrowid

        # Batch insert audited rejected rows if provided
        if filas_rechazadas:
            rech_rows = []
            for r in filas_rechazadas:
                raw_d = r.get("datos_crudos", "")
                if not isinstance(raw_d, str):
                    try:
                        raw_d = json.dumps(raw_d, ensure_ascii=False)
                    except Exception:
                        raw_d = str(raw_d)
                rech_rows.append((
                    archivo_id,
                    str(r.get("linea_origen", "")),
                    raw_d,
                    str(r.get("motivo", "rechazo_general")),
                    str(r.get("detalle_error", "")),
                    int(r.get("pagina", 1))
                ))
            conn.executemany(
                """
                INSERT INTO filas_rechazadas (
                    archivo_id, linea_origen, datos_crudos, motivo, detalle_error, pagina
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                rech_rows
            )

        return archivo_id


def get_filas_rechazadas(
    archivo_id: Optional[int] = None,
    db_path: str = DEFAULT_DB_PATH
) -> List[Dict[str, Any]]:
    """
    Recupera los registros estructurados de auditoría para filas descartadas.

    Args:
        archivo_id: ID opcional de archivo procesado. Si es None, retorna las últimas 500 filas.
        db_path: Ruta a la base de datos SQLite.

    Returns:
        List[Dict[str, Any]]: Lista de registros de auditoría.
    """
    init_db(db_path)
    with db_session(db_path) as conn:
        if archivo_id is not None:
            rows = conn.execute(
                "SELECT id, archivo_id, linea_origen, datos_crudos, motivo, detalle_error, pagina, created_at FROM filas_rechazadas WHERE archivo_id = ? ORDER BY id ASC;",
                (archivo_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, archivo_id, linea_origen, datos_crudos, motivo, detalle_error, pagina, created_at FROM filas_rechazadas ORDER BY id DESC LIMIT 500;"
            ).fetchall()
        return [dict(r) for r in rows]


def update_transaction_review(
    archivo_id: int,
    transactions: List[Transaccion],
    recon: ReconciliacionResultado,
    estado_conciliacion: str,
    db_path: str = DEFAULT_DB_PATH
) -> bool:
    """
    Actualiza en SQLite las transacciones editadas manualmente en la Cola de Revisión
    y el estado de conciliación del archivo padre.
    """
    init_db(db_path)
    dates = [tx.fecha for tx in transactions if tx.fecha and str(tx.fecha).strip()]
    rango_inicio = min(dates) if dates else None
    rango_fin = max(dates) if dates else None
    conciliado_int = 1 if recon.cuadra is True else (0 if recon.cuadra is False else None)

    s_ini = float(recon.saldo_inicial) if recon.saldo_inicial is not None else None
    s_fin = float(recon.saldo_final_esperado if recon.saldo_final_esperado is not None else (recon.saldo_final_declarado or 0.0))
    diff = float(recon.diferencia) if recon.diferencia is not None else 0.0

    with db_session(db_path) as conn:
        conn.execute(
            """
            UPDATE archivos_procesados SET
                saldo_inicial = ?,
                saldo_final = ?,
                diferencia_conciliacion = ?,
                conciliado = ?,
                estado_conciliacion = ?,
                total_transacciones = ?,
                rango_fecha_inicio = ?,
                rango_fecha_fin = ?
            WHERE id = ?;
            """,
            (
                s_ini,
                s_fin,
                diff,
                conciliado_int,
                estado_conciliacion,
                len(transactions),
                rango_inicio,
                rango_fin,
                archivo_id
            )
        )

        has_any_id = any(getattr(tx, "id", None) is not None for tx in transactions)
        if has_any_id:
            for tx in transactions:
                tx_id = getattr(tx, "id", None)
                tipo_val = tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
                if tx_id is not None:
                    conn.execute(
                        """
                        UPDATE transacciones SET
                            fecha = ?, concepto = ?, importe = ?, descripcion = ?, tipo = ?,
                            confianza = ?, origen = ?, linea_origen = ?, saldo_renglon = ?,
                            estado_revision = ?, pagina = ?
                        WHERE id = ? AND archivo_id = ?;
                        """,
                        (
                            tx.fecha,
                            tx.concepto,
                            float(tx.importe),
                            tx.descripcion,
                            tipo_val,
                            float(getattr(tx, "confianza", 1.0)),
                            str(getattr(tx, "origen", "llm")),
                            str(getattr(tx, "linea_origen", "")),
                            float(tx.saldo_renglon) if getattr(tx, "saldo_renglon", None) is not None else None,
                            str(getattr(tx, "estado_revision", "VERIFICADO")),
                            int(getattr(tx, "pagina", 1)),
                            tx_id,
                            archivo_id
                        )
                    )
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO transacciones (
                            archivo_id, fecha, concepto, importe, descripcion, tipo,
                            confianza, origen, linea_origen, saldo_renglon, estado_revision, pagina
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            archivo_id,
                            tx.fecha,
                            tx.concepto,
                            float(tx.importe),
                            tx.descripcion,
                            tipo_val,
                            float(getattr(tx, "confianza", 1.0)),
                            str(getattr(tx, "origen", "llm")),
                            str(getattr(tx, "linea_origen", "")),
                            float(tx.saldo_renglon) if getattr(tx, "saldo_renglon", None) is not None else None,
                            str(getattr(tx, "estado_revision", "VERIFICADO")),
                            int(getattr(tx, "pagina", 1))
                        )
                    )
                    tx.id = cur.lastrowid
        else:
            conn.execute("DELETE FROM transacciones WHERE archivo_id = ?;", (archivo_id,))
            tx_rows = []
            for tx in transactions:
                tipo_val = tx.tipo.value if hasattr(tx.tipo, "value") else str(tx.tipo)
                tx_rows.append((
                    archivo_id,
                    tx.fecha,
                    tx.concepto,
                    float(tx.importe),
                    tx.descripcion,
                    tipo_val,
                    float(getattr(tx, "confianza", 1.0)),
                    str(getattr(tx, "origen", "llm")),
                    str(getattr(tx, "linea_origen", "")),
                    float(tx.saldo_renglon) if getattr(tx, "saldo_renglon", None) is not None else None,
                    str(getattr(tx, "estado_revision", "VERIFICADO")),
                    int(getattr(tx, "pagina", 1))
                ))
            if tx_rows:
                conn.executemany(
                    """
                    INSERT INTO transacciones (
                        archivo_id, fecha, concepto, importe, descripcion, tipo,
                        confianza, origen, linea_origen, saldo_renglon, estado_revision, pagina
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    tx_rows
                )

    return True


def query_transactions(
    fecha_desde: Optional[str] = None,
    fecha_hasta: Optional[str] = None,
    busqueda: Optional[str] = None,
    tipo: Optional[str] = None,
    archivo_id: Optional[int] = None,
    db_path: str = DEFAULT_DB_PATH
) -> pd.DataFrame:
    """
    Executes a high-performance filtered query against stored transactions.

    Args:
        fecha_desde: Minimum date filter (YYYY-MM-DD).
        fecha_hasta: Maximum date filter (YYYY-MM-DD).
        busqueda: Keyword for case-insensitive search across concepto and descripcion.
        tipo: 'ingreso', 'egreso', or None for all.
        archivo_id: Optional ID of specific parent statement file.
        db_path: Database path.

    Returns:
        pd.DataFrame: Formatted DataFrame with canonical columns including account origin.
    """
    init_db(db_path)

    clauses = ["1=1"]
    params = []

    if fecha_desde and fecha_desde.strip():
        clauses.append("t.fecha >= ?")
        params.append(fecha_desde.strip())

    if fecha_hasta and fecha_hasta.strip():
        clauses.append("t.fecha <= ?")
        params.append(fecha_hasta.strip())

    if busqueda and busqueda.strip():
        clauses.append("(LOWER(t.concepto) LIKE ? OR LOWER(t.descripcion) LIKE ?)")
        like_term = f"%{busqueda.strip().lower()}%"
        params.extend([like_term, like_term])

    if tipo and tipo.lower() in ("ingreso", "egreso"):
        clauses.append("t.tipo = ?")
        params.append(tipo.lower())

    if archivo_id is not None and archivo_id > 0:
        clauses.append("t.archivo_id = ?")
        params.append(archivo_id)

    query = f"""
    SELECT
        t.id,
        t.fecha,
        t.concepto,
        t.importe,
        t.descripcion AS descripción,
        t.tipo,
        t.confianza,
        t.origen,
        t.linea_origen,
        t.saldo_renglon,
        t.estado_revision,
        t.pagina,
        a.nombre_archivo,
        t.archivo_id
    FROM transacciones t
    JOIN archivos_procesados a ON t.archivo_id = a.id
    WHERE {' AND '.join(clauses)}
    ORDER BY t.fecha DESC, t.id DESC;
    """

    with db_session(db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty:
        return pd.DataFrame(columns=[
            "id", "fecha", "concepto", "importe", "descripción", "tipo",
            "confianza", "origen", "linea_origen", "saldo_renglon", "estado_revision",
            "pagina", "nombre_archivo", "archivo_id"
        ])

    return df


def get_distinct_files(db_path: str = DEFAULT_DB_PATH) -> List[Dict[str, Any]]:
    """
    Retrieves the list of all processed bank statements for UI selection, preview, and auditing.

    Args:
        db_path: Database path.

    Returns:
        List[Dict[str, Any]]: List of file records sorted by ID descending.
    """
    init_db(db_path)
    query = """
    SELECT id, nombre_archivo, tipo_archivo, fecha_procesamiento,
           total_transacciones, rango_fecha_inicio, rango_fecha_fin,
           hash_sha256, ruta_archivo, tamano_bytes,
           saldo_inicial, saldo_final, diferencia_conciliacion, conciliado,
           estado_conciliacion, modo_contable, cobertura, metricas_json
    FROM archivos_procesados
    ORDER BY id DESC;
    """
    with db_session(db_path) as conn:
        cursor = conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]



def delete_processed_file(archivo_id: int, db_path: str = DEFAULT_DB_PATH) -> bool:
    """
    Deletes a processed bank statement and all its associated transactions via CASCADE,
    as well as removing its stored physical copy in data/documentos/.

    Args:
        archivo_id: Primary key ID of the statement to remove.
        db_path: Database path.

    Returns:
        bool: True if the file was found and deleted, False otherwise.
    """
    init_db(db_path)
    file_to_remove = None
    with db_session(db_path) as conn:
        row = conn.execute(
            "SELECT ruta_archivo FROM archivos_procesados WHERE id = ?;",
            (archivo_id,)
        ).fetchone()
        if row and row["ruta_archivo"]:
            file_to_remove = row["ruta_archivo"]

        cursor = conn.execute(
            "DELETE FROM archivos_procesados WHERE id = ?;",
            (archivo_id,)
        )
        success = cursor.rowcount > 0

    if success and file_to_remove and os.path.exists(file_to_remove):
        try:
            os.unlink(file_to_remove)
        except Exception:
            pass

    return success


def get_database_stats(db_path: str = DEFAULT_DB_PATH) -> Dict[str, Any]:
    """
    Calculates overall aggregated metrics across all stored files and transactions.

    Args:
        db_path: Database path.

    Returns:
        Dict[str, Any]: Global metrics (total_archivos, total_transacciones,
                        total_ingresos, total_egresos, balance_neto, fecha_min, fecha_max).
    """
    init_db(db_path)
    query = """
    SELECT
        (SELECT COUNT(*) FROM archivos_procesados) AS total_archivos,
        COUNT(t.id) AS total_transacciones,
        COALESCE(SUM(CASE WHEN t.tipo = 'ingreso' THEN t.importe ELSE 0 END), 0.0) AS total_ingresos,
        COALESCE(SUM(CASE WHEN t.tipo = 'egreso' THEN t.importe ELSE 0 END), 0.0) AS total_egresos,
        MIN(t.fecha) AS fecha_min,
        MAX(t.fecha) AS fecha_max
    FROM transacciones t;
    """
    with db_session(db_path) as conn:
        cursor = conn.execute(query)
        row = cursor.fetchone()
        if not row:
            return {
                "total_archivos": 0,
                "total_transacciones": 0,
                "total_ingresos": 0.0,
                "total_egresos": 0.0,
                "balance_neto": 0.0,
                "fecha_min": None,
                "fecha_max": None,
            }

        total_ing = float(row["total_ingresos"])
        total_eg = float(row["total_egresos"])
        return {
            "total_archivos": int(row["total_archivos"]),
            "total_transacciones": int(row["total_transacciones"]),
            "total_ingresos": total_ing,
            "total_egresos": total_eg,
            "balance_neto": round(total_ing - total_eg, 2),
            "fecha_min": row["fecha_min"],
            "fecha_max": row["fecha_max"],
        }


# =====================================================================
# BATCH CHECKPOINTING & AUTOMATIC ERROR RESUMPTION DAL
# =====================================================================

def save_batch_checkpoint(
    batch_id: str,
    total_files: int,
    completed_files: List[str],
    status: str = "in_progress",
    failed_file: Optional[str] = None,
    error_message: Optional[str] = None,
    configs: Optional[Dict[str, Any]] = None,
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """
    Saves or updates a batch processing checkpoint to allow automatic resumption
    after an error or interruption.

    Args:
        batch_id: Deterministic identifier of the batch.
        total_files: Total number of files in the batch.
        completed_files: List of file names successfully saved to database.
        status: 'in_progress', 'interrupted', or 'completed'.
        failed_file: File name where an error occurred, if any.
        error_message: Error string or traceback.
        configs: Serialized settings for the batch.
        db_path: SQLite database path.
    """
    init_db(db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    completados_json = json.dumps(completed_files)
    configs_json = json.dumps(configs or {})

    with db_session(db_path) as conn:
        conn.execute("""
            INSERT INTO lotes_checkpoints (
                batch_id, estado, archivos_totales, archivos_completados,
                archivo_fallido, ultimo_error, configuraciones, fecha_actualizacion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(batch_id) DO UPDATE SET
                estado = excluded.estado,
                archivos_totales = excluded.archivos_totales,
                archivos_completados = excluded.archivos_completados,
                archivo_fallido = excluded.archivo_fallido,
                ultimo_error = excluded.ultimo_error,
                configuraciones = excluded.configuraciones,
                fecha_actualizacion = excluded.fecha_actualizacion;
        """, (
            batch_id, status, total_files, completados_json,
            failed_file, error_message, configs_json, now_str
        ))

    # Also persist to active JSON checkpoint file for filesystem redundancy
    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        cp_file = os.path.join(db_dir, "checkpoint_active.json")
        cp_data = {
            "batch_id": batch_id,
            "estado": status,
            "archivos_totales": total_files,
            "archivos_completados": completed_files,
            "archivo_fallido": failed_file,
            "ultimo_error": error_message,
            "configuraciones": configs or {},
            "fecha_actualizacion": now_str,
        }
        with open(cp_file, "w", encoding="utf-8") as f:
            json.dump(cp_data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_batch_checkpoint(
    batch_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> Optional[Dict[str, Any]]:
    """
    Retrieves the checkpoint for a specific batch_id, or the latest active/interrupted
    checkpoint if batch_id is None.

    Args:
        batch_id: Optional batch ID to look up.
        db_path: Target database path.

    Returns:
        Optional[Dict[str, Any]]: Checkpoint dictionary if found, else None.
    """
    init_db(db_path)
    with db_session(db_path) as conn:
        if batch_id:
            row = conn.execute(
                "SELECT * FROM lotes_checkpoints WHERE batch_id = ?;", (batch_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM lotes_checkpoints WHERE estado IN ('in_progress', 'interrupted') ORDER BY fecha_actualizacion DESC LIMIT 1;"
            ).fetchone()

        if row:
            try:
                completados = json.loads(row["archivos_completados"])
            except Exception:
                completados = []
            try:
                configs = json.loads(row["configuraciones"]) if row["configuraciones"] else {}
            except Exception:
                configs = {}

            return {
                "batch_id": row["batch_id"],
                "estado": row["estado"],
                "archivos_totales": row["archivos_totales"],
                "archivos_completados": completados,
                "archivo_fallido": row["archivo_fallido"],
                "ultimo_error": row["ultimo_error"],
                "configuraciones": configs,
                "fecha_actualizacion": row["fecha_actualizacion"]
            }

    # JSON fallback check if database record not found
    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        cp_file = os.path.join(db_dir, "checkpoint_active.json")
        if os.path.exists(cp_file):
            with open(cp_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if batch_id is None or data.get("batch_id") == batch_id:
                    return data
    except Exception:
        pass

    return None


def mark_checkpoint_file_completed(
    batch_id: str,
    filename: str,
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """
    Atomically adds a filename to the completed list in the checkpoint.
    """
    cp = get_batch_checkpoint(batch_id, db_path=db_path)
    if cp:
        completed = list(cp.get("archivos_completados", []))
        if filename not in completed:
            completed.append(filename)
        total = cp.get("archivos_totales", len(completed))
        save_batch_checkpoint(
            batch_id=batch_id,
            total_files=total,
            completed_files=completed,
            status="completed" if len(completed) >= total else "in_progress",
            failed_file=None,
            error_message=None,
            configs=cp.get("configuraciones"),
            db_path=db_path
        )


def mark_checkpoint_interrupted(
    batch_id: str,
    failed_file: str,
    error_message: str,
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """
    Marks the checkpoint as interrupted with details on the failed file and error message.
    """
    cp = get_batch_checkpoint(batch_id, db_path=db_path)
    completed = cp.get("archivos_completados", []) if cp else []
    total = cp.get("archivos_totales", len(completed) + 1) if cp else 1
    configs = cp.get("configuraciones") if cp else {}

    save_batch_checkpoint(
        batch_id=batch_id,
        total_files=total,
        completed_files=completed,
        status="interrupted",
        failed_file=failed_file,
        error_message=error_message,
        configs=configs,
        db_path=db_path
    )


def clear_batch_checkpoint(
    batch_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH
) -> None:
    """
    Deletes the checkpoint record from database and disk.
    """
    init_db(db_path)
    with db_session(db_path) as conn:
        if batch_id:
            conn.execute("DELETE FROM lotes_checkpoints WHERE batch_id = ?;", (batch_id,))
        else:
            conn.execute("DELETE FROM lotes_checkpoints;")

    try:
        db_dir = os.path.dirname(os.path.abspath(db_path))
        cp_file = os.path.join(db_dir, "checkpoint_active.json")
        if os.path.exists(cp_file):
            os.unlink(cp_file)
    except Exception:
        pass

