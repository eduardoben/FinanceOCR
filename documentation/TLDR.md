# FinanceOCR - Resumen Ejecutivo (TL;DR)

FinanceOCR es una aplicación web local y orientada a la privacidad para extraer, clasificar, validar, conciliar matemáticamente, auditar y exportar transacciones financieras desde estados de cuenta bancarios en PDF (digitales y escaneados) y comprobantes/estados en XML (CFDI SAT con complementos y addendas, OFX, CAMT.053).

---

## Qué Problema Resuelve

1. **Privacidad de Datos Financieros:** Elimina el riesgo de cargar información bancaria confidencial a nubes comerciales o APIs de terceros. Todo el procesamiento corre en la máquina local (soporte para Ollama local en `127.0.0.1:11434`).
2. **Precisión en Formatos Complejos con Doble Riel:**
   - Extracción determinista pura para formatos estructurados (CFDI 3.2/3.3/4.0, Addendas bancarias, OFX, CAMT y tablas Markdown nativas de Docling).
   - Inferencia con modelos locales abiertos (Ollama) para estados de cuenta en PDF escaneados o con disposiciones libres heterogéneas.
3. **Certeza Contable Automatizada:** Realiza conciliación aritmética estricta (`Saldo Inicial + Ingresos - Egresos = Saldo Final`) y valida la cadena secuencial de saldos por renglón (`running balance chain`), alertando de inmediato si existe alguna discrepancia.
4. **Verificación Dirigida por Página:** En lugar de re-procesar documentos enteros de forma costosa, aísla automáticamente únicamente las páginas dudosas (baja confianza `< 0.75` o rupturas de saldo) y lanza una segunda pasada selectiva en alta resolución Docling `ACCURATE` a `3.0x` reconciliando ambas pasadas.
5. **Cola de Revisión Asistida en Tiempo Real:** Panel interactivo con `st.data_editor` que recalcula en vivo la conciliación matemática al editar renglones dudosos y permite guardar los cambios atómicamente en la base de datos.
6. **Tolerancia a Fallos y Checkpoints:** Puntos de control transaccionales (`lotes_checkpoints`) que permiten pausar y reanudar el procesamiento de lotes extensos ante cualquier interrupción de hardware o red local.
7. **Eficiencia de Exportación:** Permite copiar tablas de transacciones al portapapeles con formato compatible para pegar directo en Excel (Ctrl + V), o descargar archivos Excel multi-hoja (`.xlsx`) y CSV con codificación UTF-8 con BOM.
8. **Compuerta de Calidad Continua:** Arnés de regresión continua contra el Conjunto de Oro (Golden Dataset) con 0 falsos verificados y F1 >= 0.99.

---

## Flujo de Trabajo en 5 Pasos

1. **Carga y Verificación Criptográfica:**
   - El usuario sube archivos PDF o XML.
   - El sistema calcula el hash SHA-256 e identifica si ya existía en la base de datos local SQLite (`data/finanzas.db`), permitiendo omitirlo o sobrescribirlo atómicamente.
2. **Selección de Estrategia y Pasada Rápida (Fast Pass):**
   - Para PDFs: Evaluación heurística previa en `< 10ms`. Si es digital nativo, extrae texto vectorial de inmediato; si es escaneado, aplica RapidOCR ONNX con resolución 2.0x y TableFormer Fast.
   - Para XMLs: Lectura determinista mediante `lxml` reconociendo complementos SAT (Estado de Cuenta Bancario, Pagos REP, Combustibles, Nómina) y Addendas bancarias (HSBC, BBVA, etc.).
3. **Verificación Dirigida por Página (Targeted Pass):**
   - Si se detectan páginas con confianza baja o ruptura de saldo secuencial, se extrae únicamente ese subconjunto de páginas en alta resolución (3.0x ACCURATE) y se reconcilian los resultados.
4. **Conciliación Aritmética y Persistencia Transaccional:**
   - Normalización de fechas a ISO (`YYYY-MM-DD`) con detección de cruce de años y sanitización de importes.
   - Conciliación matemática de apertura vs cierre y evaluación de cadena secuencial.
   - Persistencia en SQLite en modo WAL (`transacciones` con número de página y `filas_rechazadas` para auditoría) y resguardo del archivo original en `data/documentos/`.
5. **Consulta, Auditoría y Exportación:**
   - Pestaña de Resultados inmediatos y métricas.
   - Pestaña de Cola de Revisión para corrección interactiva con recálculo de saldo en vivo.
   - Pestaña de Historial con filtros B-Tree y visor contextual del documento original.
   - Exportación directa a Excel, CSV con BOM o copiado rápido al portapapeles.

---

## Resumen Modular del Código

| Módulo | Responsabilidad Principal |
|---|---|
| `app.py` | Interfaz Streamlit, telemetría en vivo, orquestación de pasada dirigida, visor de documentos y Cola de Revisión interactiva con recálculo en vivo. |
| `parsers.py` | Motores de lectura PDF (PyPdfium2, Docling, RapidOCR), parser determinista de tablas Markdown, verificación dirigida por página y parser XML (CFDI, Addendas, OFX, CAMT). |
| `llm_client.py` | Cliente local REST para Ollama a temperatura 0.0, bounded chunking con preservación de encabezados, compactación de tablas, parseo JSON estricto y fallback heurístico. |
| `database.py` | Capa de persistencia SQLite en modo WAL, detección SHA-256, 9 índices B-Tree, migraciones idempotentes, auditoría de `filas_rechazadas` y checkpoints. |
| `models.py` | Esquemas canónicos Pydantic (`Transaccion`, `ReconciliacionResultado`), sanitización de importes, puntuación de confianza por fila (`compute_row_confidence`) y `GoldenCaseExpected`. |
| `date_utils.py` | Normalización de fechas bancarias en español a YYYY-MM-DD, extractor de año fiscal y detector de periodos multilínea con cruce de año (`extract_statement_period`). |
| `telemetry.py` | Monitor de recursos de hardware en tiempo real (CPU, RAM, GPU NVIDIA), cronómetro de etapas (`StageStopwatch`) y telemetría de verificación en alta resolución. |
| `utils.py` | Conciliación aritmética de saldos (débito/crédito), validación de cadena secuencial (`validate_running_balance_chain`), clasificador de estado y exportadores (Excel, CSV BOM, TSV JS). |
| `tests/fixtures/golden_dataset/` | Conjunto de Oro con 4 estados de cuenta canónicos de referencia para prevención de falsos positivos y compuerta de calidad continua. |

---

## Puesta en Marcha Rápida

1. **Requisitos:** Python 3.10 o superior y Ollama instalado localmente con el modelo descargado:
   ```bash
   ollama pull gemma4:12b
   ```
2. **Instalación:**
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Ejecución:**
   ```powershell
   streamlit run app.py
   ```
4. **Ejecución de Pruebas (78 tests automatizados con Pytest):**
   ```powershell
   .\.venv\Scripts\pytest.exe test_data_validation.py test_database.py test_ocr_optimization.py test_telemetry.py test_system.py -v
   ```
