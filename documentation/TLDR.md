# FinanceOCR - Resumen Ejecutivo (TL;DR)

FinanceOCR es una aplicacion web local y orientada a la privacidad para extraer, clasificar, validar, conciliar y exportar transacciones financieras desde estados de cuenta bancarios en PDF y comprobantes fiscales en XML.

---

## Que Problema Resuelve

1. **Privacidad de Datos Financieros:** Elimina el riesgo de cargar informacion bancaria sensible a nubes comerciales o APIs de terceros. Todo el procesamiento corre en la maquina local.
2. **Precision en Formatos Complejos:** Supera las limitaciones de extractores tradicionales mediante un enfoque hibrido:
   - Extraccion determinista instantanea para formatos estructurados (CFDI 3.2/3.3/4.0, Addendas bancarias, OFX, CAMT).
   - Inferencia con modelos de lenguaje local (Ollama) para estados de cuenta en PDF escaneados o con disposiciones libres.
3. **Certeza Contable Automatizada:** Realiza conciliacion aritmetica entre los saldos declarados en el documento y la sumatoria de movimientos extraidos, alertando de inmediato si existe alguna discrepancia.
4. **Tolerancia a Fallos en Lotes:** Incluye un sistema de puntos de control (checkpoints) que permite pausar y reanudar el procesamiento de lotes extensos ante cualquier interrupcion de hardware o red local.
5. **Eficiencia de Exportacion:** Permite copiar tablas de transacciones al portapapeles con formato compatible para pegar directo en Excel (Ctrl + V), o descargar archivos Excel multi-hoja (.xlsx) y CSV con codificacion UTF-8 con BOM.

---

## Flujo de Trabajo en 4 Pasos

1. **Carga y Verificacion:**
   - El usuario sube archivos PDF o XML.
   - El sistema calcula el hash SHA-256 de cada archivo e identifica si ya habia sido procesado previamente en la base de datos local SQLite (`data/finanzas.db`), permitiendo omitirlo o sobrescribirlo limpiamente.
2. **Seleccion de Estrategia y Extraccion:**
   - Para PDFs: Evaluacion heuristica previa (menor a 10ms); si es digital nativo, extrae texto vectorial directamente (10x mas veloz); si es escaneado, aplica RapidOCR ONNX con resolucion 2.5x y TableFormer.
   - Para XMLs: Lectura determinista mediante lxml reconociendo complementos SAT (Estado de Cuenta Bancario, Pagos REP, Combustibles, Nomina) y Addendas bancarias (HSBC, BBVA, etc.). Si el formato es desconocido, se transfiere estructurado a Ollama.
3. **Conciliacion y Persistencia:**
   - Se normalizan las fechas a formato estandar ISO (`YYYY-MM-DD`) y los importes a valores positivos limpios.
   - Se valida el balance matematico (`Saldo Inicial + Ingresos - Egresos = Saldo Final`).
   - Se resguardan los registros en SQLite con soporte para consultas concurrentes (modo WAL) y se guarda una copia del archivo fisico en `data/documentos/`.
4. **Consulta y Exportacion:**
   - Consulta historica con filtros por fecha, tipo de movimiento, palabra clave y estado de cuenta origen.
   - Visualizacion de paginas PDF originales o codigo XML directamente en la interfaz.
   - Exportacion inmediata a Excel, CSV o copiado al portapapeles.

---

## Resumen Modular del Codigo

| Modulo | Responsabilidad Principal |
|---|---|
| `app.py` | Interfaz Streamlit, gestion de estado, telemetria en vivo, visor de documentos y auditoria. |
| `parsers.py` | Motores de lectura PDF (PyPdfium2, Docling, RapidOCR) y XML (CFDI, Addendas, OFX, CAMT). |
| `llm_client.py` | Cliente local REST para Ollama, compactacion de tablas, bounded chunking y manejo de reintentos. |
| `database.py` | Capa de persistencia SQLite en modo WAL, deteccion de duplicados SHA-256 y gestion de checkpoints. |
| `models.py` | Esquemas canonicos Pydantic (`Transaccion`, `ReconciliacionResultado`) y sanitizacion de importes. |
| `date_utils.py` | Normalizacion de fechas bancarias en espanol a formato estandar YYYY-MM-DD y detector de ano. |
| `telemetry.py` | Monitor de recursos de hardware en tiempo real (CPU, RAM, GPU NVIDIA) y cronometro de etapas. |
| `utils.py` | Formateo para exportacion (Excel multi-hoja, CSV BOM, TSV con JS) y motor de conciliacion de saldos. |

---

## Puesta en Marcha Rapida

1. **Requisitos:** Python 3.10 o superior y Ollama instalado localmente con el modelo descargado:
   ```bash
   ollama pull gemma4:12b
   ```
2. **Instalacion:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Ejecucion:**
   ```powershell
   streamlit run app.py
   ```
4. **Ejecucion de Pruebas (50 tests automatizados):**
   ```powershell
   python -m unittest discover -v
   ```
