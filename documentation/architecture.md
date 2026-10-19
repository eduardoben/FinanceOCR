# Arquitectura del Sistema FinanceOCR

Este documento describe la arquitectura técnica, el modelo de datos, la interacción entre componentes, los patrones de diseño y los mecanismos de verificación matemática implementados en **FinanceOCR**.

---

## 1. Filosofía de Diseño y Principios Rectores

FinanceOCR está diseñado para operaciones financieras críticas y auditoría contable bajo cinco principios inmutables:

1. **Privacidad Absoluta (Zero Cloud Egress):**
   - El procesamiento de documentos bancarios, comprobantes fiscales y estados de cuenta se ejecuta estrictamente en el entorno local del usuario.
   - Cero llamadas a APIs externas comerciales y cero telemetría externa.
   - La inferencia de lenguaje natural se apoya exclusivamente en instancias locales de Ollama sobre `127.0.0.1:11434`.

2. **Arquitectura Híbrida de Doble Riel (Deterministic First, LLM Fallback):**
   - Si un documento cuenta con estructura predecible (XML CFDI, complementos SAT, addendas bancarias, OFX, CAMT o tablas Markdown nativas de Docling), el sistema aplica extracción determinista pura con latencia sub-segundo y precisión matemática del 100%.
   - La inferencia LLM se reserva para disposiciones visuales complejas, formatos heterogéneos o textos con OCR ruidoso.

3. **Certeza Contable y Verificación Aritmética:**
   - La validez de una extracción no se asume ciegamente; se somete a conciliación aritmética estricta contra los saldos de apertura y cierre declarados en el documento (`Saldo Inicial + Ingresos - Egresos = Saldo Final`).
   - Cada renglón extraído se valida contra la secuencia de saldos acumulados (`running balance chain`), penalizando saltos o discrepancias.

4. **Verificación Dirigida por Página (Targeted High-Res Re-extraction):**
   - El sistema no penaliza el rendimiento re-procesando documentos completos en alta resolución.
   - En su lugar, aísla automáticamente únicamente las páginas dudosas (páginas con confianza baja `< 0.75`, rupturas de saldo o rechazos de esquema) y lanza una segunda pasada dirigida con Docling `ACCURATE` a `3.0x`, reconciliando ambas pasadas mediante arbitraje matemático.

5. **Tolerancia a Fallos, Auditoría y Reanudación Transaccional:**
   - Los lotes extensos cuentan con puntos de control (`lotes_checkpoints`) transaccionales y redundancia física. Si ocurre una interrupción de hardware, el trabajo previo no se pierde y se reanuda al instante.
   - Toda fila que no cumple con las reglas contables no se descarta silenciosamente; se almacena en `filas_rechazadas` para auditoría y revisión en la interfaz.

---

## 2. Diagrama de Arquitectura y Flujo de Interacción

```mermaid
flowchart TD
    subgraph UI["Capa de Presentacion (app.py)"]
        UI_Upload[Carga Masiva de Archivos PDF y XML]
        UI_Strategy[Selector de Estrategias y Parametros]
        UI_Telemetry[Monitor de Telemetria y Cronometro de Etapas]
        UI_Results[Pestana 1: Resultados, Metricas y Conciliacion]
        UI_Review[Pestana 2: Cola de Revision Interactiva st.data_editor]
        UI_History[Pestana 3: Historial Persistente y Filtros B-Tree]
        UI_Inspector[Visor Contextual de Documentos PDF y XML]
        UI_Export[Exportadores: Excel Multi-Hoja, TSV Portapapeles, CSV BOM]
    end

    subgraph INGESTION["Capa de Ingestion y Pasada 1: Fast Pass (parsers.py)"]
        SHA256[Identificador Criptografico SHA-256]
        PreFlight[Pre-flight Heuristico pypdfium2 menor a 10ms]
        PDF_Fast[PyPdfium2 / Docling TableFormer FAST Escala 2.0x]
        XML_Parser[Parser Determinista lxml: CFDI SAT, ECB, REP, Addendas]
        OFX_CAMT[Parsers Especializados OFX / CAMT.053]
    end

    subgraph DETERMINISTIC_TABLES["Parser Determinista de Tablas Markdown (parsers.py)"]
        TableDetect[Deteccion de Bloques |...| y Normalizacion de Headers]
        MultilineMerge[Fusion de Descripciones Multilinea]
        RowScoring[Calculo de Confianza por Fila compute_row_confidence]
        ChainValidator[Validacion de Cadena de Saldos validate_running_balance_chain]
    end

    subgraph LLM_PIPELINE["Capa de Inferencia Local Ollama (llm_client.py)"]
        Compaction[Compactacion de Espacios en Blanco]
        Chunking[Bounded Chunking con Preservacion de Encabezados]
        CandidateFilter[Filtro Regex Pre-flight de Filas Financieras]
        OllamaREST[Cliente REST Ollama Temp 0.0 y Thinking Support]
        JsonCleaner[Limpieza Estricta y Validador JSON con Fallback Heuristico]
        BoundaryDedup[Desduplicacion de Filas en Fronteras de Chunks]
    end

    subgraph TARGETED_VERIFICATION["Verificacion Dirigida: Pasada 2 (parsers.py y telemetry.py)"]
        PageInspector[Deteccion de Paginas Dudosas identify_low_confidence_pages]
        HighResPass[Re-extraccion Selectiva 3.0x ACCURATE reextract_pages_high_res]
        PassArbitration[Arbitraje y Reconciliacion de Pasadas reconcile_extraction_passes]
        HighResTelemetry[Telemetria de Alta Resolucion record_high_res_verification]
    end

    subgraph CERTEZA["Capa de Certeza Contable y Normalizacion (models.py y utils.py)"]
        DateNorm[Normalizador de Fechas en Espanol y Periodo Fiscal]
        ImporteSanitize[Sanitizador de Importes Contables models.py]
        BalanceReconcile[Conciliador Aritmetica de Saldos reconcile_transactions]
        DocClassifier[Clasificador de Estado: VERIFICADO, REVISAR, RECHAZADO]
    end

    subgraph PERSISTENCE["Capa de Persistencia SQLite WAL (database.py)"]
        DAL_Session[Manejador de Contexto db_session con WAL]
        Tab_Archivos[archivos_procesados: metadatos, saldos y metricas]
        Tab_Trans[transacciones: registros con pagina y origen]
        Tab_Rechazadas[filas_rechazadas: auditoria de anomalias]
        Tab_Checkpoints[lotes_checkpoints: recuperacion de lotes]
        DocStore[Almacen Fisico Seguro data/documentos/]
    end

    subgraph QUALITY_GATE["Arnes de Regresion Continua y Conjunto de Oro"]
        GoldenCases[Golden Dataset: 4 Estados de Cuenta Benchmark]
        EvalHarness[Evaluador evaluate_against_golden: Precision, Recall, F1]
        SafetyGate[Compuerta de Calidad: 0 Falsos Verificados y F1 >= 0.99]
    end

    %% Relaciones de flujo
    UI_Upload --> SHA256
    SHA256 --> DAL_Session
    UI_Strategy --> PreFlight

    PreFlight -->|Digital Nativo / Escaneo| PDF_Fast
    UI_Upload -->|Comprobantes XML| XML_Parser
    UI_Upload -->|Extractos Bancarios XML| OFX_CAMT

    PDF_Fast --> TableDetect
    TableDetect --> MultilineMerge
    MultilineMerge --> RowScoring
    RowScoring --> ChainValidator

    PDF_Fast -->|Tablas Complejas / No Tabulares| Compaction
    Compaction --> Chunking
    Chunking --> CandidateFilter
    CandidateFilter -->|Bloque Financiero| OllamaREST
    OllamaREST --> JsonCleaner
    JsonCleaner --> BoundaryDedup
    BoundaryDedup --> RowScoring

    ChainValidator --> PageInspector
    PageInspector -->|Paginas Dudosas Detectadas| HighResPass
    HighResPass --> PassArbitration
    PassArbitration --> HighResTelemetry
    PassArbitration --> BalanceReconcile

    PageInspector -->|Sin Anomalias| BalanceReconcile
    XML_Parser --> BalanceReconcile
    OFX_CAMT --> BalanceReconcile

    BalanceReconcile --> DocClassifier
    DocClassifier --> DAL_Session

    DAL_Session --> Tab_Archivos
    DAL_Session --> Tab_Trans
    DAL_Session --> Tab_Rechazadas
    DAL_Session --> Tab_Checkpoints
    DAL_Session --> DocStore

    Tab_Trans --> UI_Results
    Tab_Trans --> UI_Review
    Tab_Trans --> UI_History
    Tab_Rechazadas --> UI_Review
    DocStore --> UI_Inspector

    UI_Review -->|Modificacion y Recalculo en Vivo| DAL_Session
    UI_History --> UI_Export

    GoldenCases --> EvalHarness
    EvalHarness --> SafetyGate
```

---

## 3. Descomposición Modular del Sistema

### 3.1. `app.py` (Capa de Presentación y Orquestación)
Punto de entrada de la aplicación construido sobre Streamlit siguiendo las directrices de `.agents/skills/developing-with-streamlit/SKILL.md`:
- **Barra Lateral y Configuración:**
  - Control de conexión al servidor Ollama, selector de modelos y ventana de contexto.
  - Selección de estrategias predeterminadas para PDF y XML con captura del RFC del titular para inversión contable automática.
- **Pestaña 1: Extractor y Procesamiento:**
  - Telemetría en tiempo real: Carga de CPU, memoria RAM, GPU NVIDIA y cronómetro modular por etapas.
  - Carga masiva con verificación hash SHA-256 instantánea y tarjetas de reanudación ante checkpoints activos.
  - Acordeones contextuales por archivo para asignación individual de estrategias, escala OCR y rangos de página con vista previa visual.
  - Disparo transparente de la **Verificación Dirigida por Página** en documentos PDF con anomalías.
  - Tarjetas de resultado con métricas contables, informe visual de conciliación y descargas inmediatas.
- **Pestaña 2: ⚠️ Cola de Revisión (Nueva en Fase 4):**
  - Panel interactivo de auditoría y corrección asistida basado en `st.data_editor`.
  - Filtro dinámico para aislar transacciones en estado `REVISAR`, con baja confianza (`< 0.75`), con advertencias contables o filas rechazadas.
  - **Recálculo Aritmético en Vivo:** Al modificar importes, fechas o tipos en la tabla interactiva, la interfaz recalcula instantáneamente la conciliación de saldos en memoria.
  - **Persistencia Transaccional:** Botón de guardado que actualiza de forma atómica los registros en la base de datos SQLite y promueve las filas corregidas a estado `VERIFICADO`.
- **Pestaña 3: Historial y Consulta de Transacciones:**
  - Búsqueda indexada sobre SQLite con filtros cruzados de fechas, tipo de flujo, estado de cuenta y buscador textual.
  - Visor contextual de documentos almacenados físicamente en disco.
  - Exportación en formatos profesionales: Excel multi-hoja (`openpyxl`), CSV con BOM (`utf-8-sig`) y botón interactivo JavaScript para copiado TSV al portapapeles.

---

### 3.2. `parsers.py` (Motores de Extracción Híbridos y Pasada Dirigida)
Encapsula la lógica de conversión física y sintáctica de documentos a entidades contables:
- **Pre-flight Heurístico (`detect_pdf_profile`):**
  - Evalúa la densidad vectorial de caracteres mediante `pypdfium2` en menos de 10ms. Si supera 60 caracteres/página, se enruta como digital nativo; de lo contrario, se clasifica como escaneo rasterizado.
- **Caché Singleton de Convertidores (`get_cached_converter`):**
  - Mantiene en memoria una instancia reutilizable de convertidores Docling/RapidOCR indexada por parámetros para evitar recargas costosas de pesos y tokenizadores ONNX.
- **Parser Determinista de Tablas Markdown (`parse_docling_markdown_tables`):**
  - Detecta bloques tabulares Markdown (`|...|`) generados por TableFormer.
  - Normaliza encabezados mediante expresiones regulares para identificar columnas clave: `fecha`, `concepto/descripcion`, `cargo/retiro`, `abono/deposito`, `saldo`.
  - Fusiona automáticamente descripciones multilínea preservando la continuidad de renglones rotos.
  - Asigna el número de página de origen (`pagina: int`) a cada transacción extraída.
- **Aislamiento de Páginas Dudosas (`identify_low_confidence_pages`):**
  - Analiza las transacciones de la primera pasada e identifica páginas con confianza promedio `< 0.75`, transacciones con `estado_revision == "REVISAR"`, renglones con rupturas de saldo o presencia de filas rechazadas.
  - Si la cobertura global es baja (`< 0.60`), incluye todas las páginas para garantizar exhaustividad.
- **Re-extracción en Alta Resolución (`reextract_pages_high_res`):**
  - Re-procesa estrictamente el subconjunto de páginas dudosas utilizando Docling con `TableFormerMode.ACCURATE` y escala de renderizado elevada a `3.0x`.
- **Arbitraje y Reconciliación de Pasadas (`reconcile_extraction_passes`):**
  - Compara las transacciones de la pasada 1 contra la pasada 2 por coincidencia de llave transaccional `(fecha, importe)`.
  - Si la pasada 2 resuelve la ruptura del saldo secuencial, sustituye las transacciones dudosas, promueve su estado a `VERIFICADO` y asigna `origen="verificado_multipass"`.
  - Si la contradicción persiste, marca las transacciones involucradas con `estado_revision="DISCREPANCIA_OCR"` y degrada su confianza a `0.35`.
- **Parser Determinista XML (`parse_xml`):**
  - Parser seguro con `lxml.etree` que decodifica CFDI SAT (3.2, 3.3, 4.0), Complemento de Estado de Cuenta Bancario (`ecb:EstadoDeCuentaBancario`), Complemento de Pagos (`pago20:Pagos`), Addendas HSBC (débito y crédito), OFX y CAMT.053 ISO 20022.

---

### 3.3. `llm_client.py` (Cliente REST Local y Pipeline de Inferencia)
Gestiona la comunicación con Ollama y la transformación de texto libre:
- **Compactación de Espacios (`compact_markdown_tables`):**
  - Reduce el espaciado excesivo de tablas Markdown en más de un 80%, optimizando radicalmente la ventana de contexto.
- **Bounded Chunking con Preservación de Encabezados (`chunk_text`):**
  - Segmenta el texto en bloques acotados (`max_chunk_chars=1800`, `max_table_rows=6`), reinyectando los encabezados originales en fragmentos divididos para evitar la inversión de cargos y abonos.
- **Filtro Pre-Flight de Filas Financieras (`chunk_has_transaction_rows`):**
  - Evalúa la concurrencia de fechas y montos en `< 1ms`, descartando avisos de privacidad y publicidad antes de invocar a Ollama.
- **Inferencia Determinista con Temperatura 0.0:**
  - Configurado a temperatura `0.0` para garantizar reproducibilidad matemática y mitigar alucinaciones contables.
- **Parseo Resiliente de JSON (`clean_and_parse_llm_json` y `parse_strict_llm_json`):**
  - Filtra etiquetas de razonamiento (`<think>...</think>`), extrae bloques delimitados por Markdown y aplica expresiones regulares de rescate ante respuestas incompletas.
- **Respaldo Heurístico (`parse_heuristic_fallback`):**
  - En caso de indisponibilidad de Ollama tras 3 reintentos con retroceso exponencial, activa un extractor heurístico/regex determinista como salvaguarda.

---

### 3.4. `database.py` (Capa de Acceso a Datos y Persistencia)
Provee persistencia confiable, libre de bloqueos y totalmente auditable:
- **Modo WAL y Concurrencia:**
  - Ejecuta SQLite con `PRAGMA journal_mode = WAL;`, `PRAGMA synchronous = NORMAL;` y `PRAGMA foreign_keys = ON;`, permitiendo lecturas concurrentes sin colisionar con Streamlit.
- **Manejador de Sesión Seguro (`db_session`):**
  - Context manager con confirmación (`commit`) y reversión automática (`rollback`) ante fallos.
- **Detección Criptográfica de Duplicados (`compute_file_hash`):**
  - Genera firmas SHA-256 binarias para evitar reprocesamientos redundantes y permitir sobrescritura atómica (`overwrite=True`).
- **Resguardo Físico Permanente:**
  - Almacena copias de los archivos en `data/documentos/{hash[:16]}_{safe_name}` con eliminación sincronizada en cascada al suprimir el registro de base de datos.
- **Auditoría de Filas Rechazadas (`filas_rechazadas`):**
  - Registra renglones que no superaron la validación de esquema o presentaron montos en cero, almacenando línea de origen, datos crudos, motivo, detalle del error y página.
- **Trazabilidad por Página (`pagina`):**
  - Persiste la columna `pagina` en la tabla `transacciones`, facilitando la navegación visual del auditor hacia la página exacta del documento original.
- **Gestión de Puntos de Control (`lotes_checkpoints`):**
  - Monitorea el progreso de lotes y garantiza la recuperación inmediata ante fallas o cierres inesperados.
- **Migraciones Automáticas Idempotentes:**
  - Inspecciona el esquema al inicializar y aplica `ALTER TABLE` de forma segura y no destructiva si detecta columnas faltantes.

---

### 3.5. `models.py` (Modelos Canónicos, Validación y Puntuación)
Define los contratos de datos mediante Pydantic v2:
- **`TipoTransaccion` (Enum):**
  - Define estrictamente los tipos de flujo contable: `ingreso` y `egreso`.
- **`Transaccion` (BaseModel):**
  - Campos: `fecha` (YYYY-MM-DD), `concepto`, `importe` (positivo estricto), `descripcion`, `tipo`, `confianza` (0.0 a 1.0), `origen`, `linea_origen`, `saldo_renglon`, `estado_revision`, `pagina` (int).
  - `@field_validator("importe", mode="before")`: Sanitiza formatos bancarios complejos (paréntesis de débito, signos posteriores, sufijos `CR`/`DR`, monedas) a flotante positivo.
- **Puntuación de Confianza por Fila (`compute_row_confidence`):**
  - Asigna una base de confianza según el origen:
    * `xml`: `1.00`
    * `determinista_tabla`: `0.90`
    * `verificado_multipass`: `0.85`
    * `llm`: `0.70`
    * `desconocido`: `0.50`
  - Bonificación: `+0.15` si la cadena de saldo cuadra matemáticamente (`cadena_ok=True`).
  - Penalizaciones acumulativas: `-0.15` por concepto truncado o genérico, `-0.20` por fecha no ISO, `-0.20` por ruptura de saldo (`cadena_ok=False`).
  - Promoción/Degradación: Si la confianza resultante es `>= 0.85`, se clasifica como `VERIFICADO`; de lo contrario, se marca como `REVISAR`.
- **Auditoría Estructurada (`auditar_fila_rechazada`):**
  - Modela registros para la tabla `filas_rechazadas` garantizando trazabilidad completa de descartes.
- **Contrato de Caso de Oro (`GoldenCaseExpected`):**
  - Estructura canónica para validar el benchmark de regresión continua contra verdad absoluta.

---

### 3.6. `date_utils.py` (Normalización y Resolución de Periodos)
- **`normalize_spanish_date`:**
  - Convierte más de 15 formatos bancarios en español a ISO 8601 (`YYYY-MM-DD`). Reconoce los 12 meses (nombres y abreviaturas), conectores gramaticales (`de`, `del`), formatos numéricos y valida la existencia en el calendario gregoriano.
- **`extract_statement_year`:**
  - Extrae el año fiscal de corte (2015-2035) desde los encabezados del documento para resolver fechas bancarias sin año.
- **`extract_statement_period`:**
  - Inspecciona los encabezados del estado de cuenta (ej. `PERIODO: 18 DIC 2023 AL 15 ENE 2024`) para detectar cruces de año fiscal. Permite asignar con precisión el año correcto a transacciones de diciembre (`2023`) y enero (`2024`) en un mismo estado de cuenta.

---

### 3.7. `telemetry.py` (Monitoreo de Hardware y Cronometría)
- **`get_system_metrics`:**
  - Captura métricas locales en menos de 50ms sin llamadas externas: carga y núcleos de CPU vía `psutil`, uso de RAM y telemetría de GPUs NVIDIA mediante `nvidia-smi` en subproceso seguro.
- **`StageStopwatch`:**
  - Cronómetro modular que mide con precisión de milisegundos los tiempos por fase: extracción documental, inferencia LLM, verificación dirigida y persistencia.
- **Telemetría de Verificación Dirigida:**
  - `record_high_res_verification` y `get_high_res_verification_telemetry`: Registra páginas re-procesadas, tiempo transcurrido en alta resolución y transacciones corregidas por el arbitraje multi-pasada.

---

### 3.8. `utils.py` (Conciliación Aritmética y Exportación)
- **Conciliación Aritmética (`reconcile_transactions`):**
  - Resuelve simultáneamente las ecuaciones de cuenta de cheques/débito y de tarjeta de crédito, seleccionando el modo con menor discrepancia matemática y aplicando una tolerancia estricta (`<= $0.05`).
- **Validación de Cadena de Saldos (`validate_running_balance_chain`):**
  - Recorre cronológicamente las transacciones que contienen saldo por renglón (`saldo_renglon`), verificando que `Saldo[i] = Saldo[i-1] ± Importe[i]`. Identifica con precisión de índice cualquier salto o inconsistencia.
- **Clasificador de Estado de Documento (`clasificar_estado_documento`):**
  - Clasifica el estado general del documento en `VERIFICADO`, `REVISAR` o `RECHAZADO` según la conciliación de saldos, la tasa de confianza promedio y la cobertura de captura.
- **Exportadores:**
  - Generación de libros Excel multi-hoja (`openpyxl`), cadenas TSV para el portapapeles y archivos CSV con BOM (`utf-8-sig`).

---

### 3.9. Arnés de Evaluación y Conjunto de Oro (`tests/fixtures/golden_dataset/`)
Módulo de aseguramiento de calidad y prevención de regresiones contables:
- **Casos de Referencia Canónicos (Golden Dataset):**
  1. `case_01_bbva_debito_digital`: Débito digital con cadena de saldos perfecta.
  2. `case_02_banamex_credito_comisiones`: Tarjeta de crédito con intereses, IVA de comisiones, compras y pagos.
  3. `case_03_santander_fechas_sin_anio_oxxo_gemelas`: Fechas sin año con salto de periodo anual (2023->2024) y compras gemelas idénticas preservadas sin sobre-desduplicación.
  4. `case_04_hsbc_sobregiro_negativo`: Cuenta de cheques con sobregiro y saldos negativos.
- **Evaluador de Verdad Absoluta (`evaluate_against_golden`):**
  - Calcula métricas rigurosas:
    * $\text{Precisión de Filas} = \frac{\text{TP}}{\max(\text{TP} + \text{FP}, 1)}$
    * $\text{Recall de Filas} = \frac{\text{TP}}{\max(\text{TP} + \text{FN}, 1)}$
    * $\text{F1 Filas} = \frac{2 \cdot P \cdot R}{\max(P + R, 10^{-9})}$
    * $\text{Exactitud de Tipo} = \frac{\text{Aciertos de Tipo Flujo}}{\text{Total Coincidencias}}$
    * **Compuerta Anti Falsos Verificados (`falso_verificado`):** Se activa como `True` (fallo crítico) si el documento fue calificado como `VERIFICADO` pero $F1 < 1.0$ o $\text{Exactitud de Tipo} < 1.0$.
  - Los 4 casos del Golden Dataset alcanzan $F1 = 1.0$, exactitud de tipo del $100\%$ y $0$ falsos verificados.

---

## 4. Esquema Relacional de Base de Datos

```
+--------------------------------------------------------------------------+
|                          archivos_procesados                             |
+--------------------------------------------------------------------------+
| id                       INTEGER PRIMARY KEY AUTOINCREMENT               |
| nombre_archivo           TEXT NOT NULL                                   |
| hash_sha256              TEXT NOT NULL UNIQUE                            |
| tipo_archivo             TEXT NOT NULL ('pdf' | 'xml')                   |
| fecha_procesamiento      TEXT NOT NULL (YYYY-MM-DD HH:MM:SS)             |
| total_transacciones      INTEGER NOT NULL DEFAULT 0                      |
| rango_fecha_inicio       TEXT (YYYY-MM-DD)                               |
| rango_fecha_fin          TEXT (YYYY-MM-DD)                               |
| ruta_archivo             TEXT                                            |
| tamano_bytes             INTEGER                                         |
| saldo_inicial            REAL                                            |
| saldo_final              REAL                                            |
| diferencia_conciliacion  REAL                                            |
| conciliado               INTEGER (1, 0, NULL)                            |
| estado_conciliacion      TEXT ('VERIFICADO', 'REVISAR', 'RECHAZADO')     |
| modo_contable            TEXT ('debito', 'credito', 'ambiguo')           |
| cobertura                REAL                                            |
| metricas_json            TEXT (JSON con telemetria y cronometria)        |
+--------------------------------------------------------------------------+
                                     |
                                     | 1
                                     |
            +------------------------+------------------------+
            | N (ON DELETE CASCADE)                           | N (ON DELETE CASCADE)
            v                                                 v
+--------------------------------------+   +------------------------------------+
|            transacciones             |   |          filas_rechazadas          |
+--------------------------------------+   +------------------------------------+
| id              INTEGER PK AUTOINC   |   | id            INTEGER PK AUTOINC   |
| archivo_id      INTEGER NOT NULL FK  |   | archivo_id    INTEGER NOT NULL FK  |
| fecha           TEXT NOT NULL        |   | linea_origen  TEXT                 |
| concepto        TEXT NOT NULL        |   | datos_crudos  TEXT                 |
| importe         REAL NOT NULL        |   | motivo        TEXT                 |
| descripcion     TEXT                 |   | detalle_error TEXT                 |
| tipo            TEXT NOT NULL        |   | pagina        INTEGER DEFAULT 1    |
| confianza       REAL DEFAULT 1.0     |   | created_at    TIMESTAMP DEFAULT NOW|
| origen          TEXT DEFAULT 'llm'   |   +------------------------------------+
| linea_origen    TEXT DEFAULT ''      |
| saldo_renglon   REAL                 |
| estado_revision TEXT DEFAULT 'pend'  |
| pagina          INTEGER DEFAULT 1    |
+--------------------------------------+

+--------------------------------------------------------------------------+
|                           lotes_checkpoints                              |
+--------------------------------------------------------------------------+
| batch_id                 TEXT PRIMARY KEY                                |
| estado                   TEXT NOT NULL ('in_progress','interrupted',...) |
| archivos_totales         INTEGER NOT NULL                                |
| archivos_completados     TEXT NOT NULL (JSON Array)                      |
| archivo_fallido          TEXT                                            |
| ultimo_error             TEXT                                            |
| configuraciones          TEXT (JSON Object)                              |
| fecha_actualizacion      TEXT NOT NULL (YYYY-MM-DD HH:MM:SS)             |
+--------------------------------------------------------------------------+
```

### DDL y Optimización de Índices B-Tree

```sql
-- Indices B-Tree para consultas de alta velocidad (<5ms)
CREATE INDEX IF NOT EXISTS idx_transacciones_fecha ON transacciones(fecha);
CREATE INDEX IF NOT EXISTS idx_transacciones_concepto ON transacciones(concepto);
CREATE INDEX IF NOT EXISTS idx_transacciones_tipo ON transacciones(tipo);
CREATE INDEX IF NOT EXISTS idx_transacciones_archivo ON transacciones(archivo_id);
CREATE INDEX IF NOT EXISTS idx_transacciones_pagina ON transacciones(pagina);
CREATE INDEX IF NOT EXISTS idx_filas_rechazadas_archivo ON filas_rechazadas(archivo_id);
CREATE INDEX IF NOT EXISTS idx_archivos_nombre ON archivos_procesados(nombre_archivo);
CREATE INDEX IF NOT EXISTS idx_archivos_hash ON archivos_procesados(hash_sha256);
CREATE INDEX IF NOT EXISTS idx_checkpoints_estado ON lotes_checkpoints(estado);
```

---

## 5. Modelo de Seguridad, Integridad y Auditoría

1. **Aislamiento de Red:** Todo el flujo opera en memoria y almacenamiento local. Ningún dato transaccional es transmitido al exterior.
2. **Inmunidad a Inyección SQL:** Todas las consultas en `database.py` utilizan sentencias preparadas parametrizadas (`?`); no existe interpolación dinámica de cadenas.
3. **Integridad Referencial Estricta:** La aplicación activa `PRAGMA foreign_keys = ON;`. La eliminación de un estado de cuenta padre borra en cascada sus transacciones hijas, sus filas rechazadas y su copia física en disco de forma sincronizada.
4. **Protección contra Bloqueos Concurrente:** El modo SQLite WAL (`Write-Ahead Logging`) garantiza que las consultas de auditoría en la interfaz no bloqueen las inserciones por lote.
5. **Auditoría Completa de Descarte:** Ningún dato se pierde silenciosamente; todo movimiento dudoso se califica con su confianza respectiva o se archiva en `filas_rechazadas` con el motivo exacto para revisión humana.
6. **Compuerta de Calidad Continua:** La suite de 78 pruebas unitarias y de integración garantiza que futuras refactorizaciones mantengan una tasa cero de falsos verificados.
