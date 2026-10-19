# Conjunto de Oro de Validación Contable (Golden Dataset)

Este directorio contiene los casos de referencia verificados contra la "verdad absoluta" (ground truth) para la compuerta de calidad y el arnés de regresión continua de `FinanceOCR`.

## Estructura de un Caso de Prueba

Cada caso consta de un subdirectorio con dos artefactos obligatorios:
1. `documento.md` (o `.xml`, `.pdf`): El texto, tabla o archivo original anonimizado.
2. `expected.json`: La verdad absoluta estructurada según el esquema `GoldenCaseExpected` de `models.py`.

### Esquema `expected.json` (Pydantic `GoldenCaseExpected`)

```json
{
  "case_id": "nombre_identificador_del_caso",
  "banco": "Institución Emisora (ej. BBVA, Citibanamex, Santander, HSBC)",
  "tipo_documento": "pdf_digital | pdf_escaneado | xml",
  "modo_esperado": "debito | credito",
  "saldo_inicial": 15000.00,
  "saldo_final": 22350.00,
  "total_cargos": 7650.00,
  "total_abonos": 15000.00,
  "movimientos_esperados": [
    {
      "fecha": "YYYY-MM-DD",
      "concepto": "Nombre limpio del comercio o concepto",
      "importe": 1250.00,
      "descripcion": "Detalles completos, folios o autorizaciones",
      "tipo": "ingreso | egreso",
      "saldo_renglon": 28750.00,
      "pagina": 1
    }
  ]
}
```

## Casos Base Incluidos

1. **`case_01_bbva_debito_digital`**:
   - Cuenta de débito digital en pesos (BBVA).
   - Verificación de cadena de saldos secuencial perfecta.
2. **`case_02_banamex_credito_comisiones`**:
   - Tarjeta de crédito (Citibanamex) con intereses y comisiones por anualidad.
   - Valida la fórmula contable de tarjeta de crédito: `saldo_final = saldo_inicial - abonos + cargos`.
3. **`case_03_santander_fechas_sin_anio_oxxo_gemelas`**:
   - Estado de cuenta con fechas mexicanas sin año (ej. `18 DIC`, `05 ENE`) que cruzan de 2023 a 2024.
   - Compras gemelas legítimas el mismo día en OXXO por idéntico importe que deben ser preservadas íntegramente.
4. **`case_04_hsbc_sobregiro_negativo`**:
   - Cuenta de cheques con sobregiro (saldos inicial y final negativos).
   - Valida el tratamiento de saldos deudores (DR/DB/paréntesis).

## Guía para Agregar Nuevos Casos

1. **Anonimización Estricta**:
   - Reemplaza nombres de personas por titulares genéricos (ej. `JUAN PEREZ LOPEZ`).
   - Reemplaza números de cuenta, CLABE o tarjetas por números ficticios (ej. `0123456789`).
   - Elimina cualquier RFC real o información sensible personal.

2. **Verificación Matemática**:
   - En cuentas de débito: `round(saldo_inicial + total_abonos - total_cargos, 2) == round(saldo_final, 2)`.
   - En tarjetas de crédito: `round(saldo_inicial - total_abonos + total_cargos, 2) == round(saldo_final, 2)`.
   - La suma de `importe` de todos los movimientos de tipo `egreso` debe ser exactamente igual a `total_cargos`.
   - La suma de `importe` de todos los movimientos de tipo `ingreso` debe ser exactamente igual a `total_abonos`.

3. **Ejecución del Arnés de Regresión**:
   - Ejecuta:
     ```bash
     pytest test_system.py -k test_golden_dataset_regression -v
     ```
   - El test asegura:
     - `falso_verificado == False`
     - `f1_filas >= 0.99`
     - `exactitud_tipo == 1.0`
