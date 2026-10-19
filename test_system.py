"""
test_system.py - Comprehensive Unit & Integration Tests

Validates:
1. Pydantic schema validation & amount sanitization
2. Spanish banking date normalizer
3. Deterministic CFDI 4.0 XML parsing
4. Multi-sheet Excel and TSV export generation
5. Ollama structured schema conformance
"""

import os
import json
import tempfile
import unittest
from typing import Dict, Any, List, Tuple, Optional
import pandas as pd
from lxml import etree

from models import (
    Transaccion,
    TipoTransaccion,
    TransaccionesResponse,
    ReconciliacionResultado,
    clasificar_estado_documento,
    GoldenCaseExpected,
)
from date_utils import normalize_spanish_date
from parsers import (
    parse_xml,
    extract_statement_balances,
    infer_statement_mode,
    parse_docling_markdown_tables,
    validate_running_balance_chain,
)
from utils import (
    create_standard_dataframe,
    df_to_excel_bytes,
    df_to_tsv,
    df_to_csv_bytes,
    reconcile_transactions,
)


class TestFinancialExtractor(unittest.TestCase):

    def test_transaccion_model_sanitization(self):
        """Verifies that currency symbols, commas, and negative values sanitize cleanly."""
        tx = Transaccion(
            fecha="2024-03-15",
            concepto="Pago Supermercado",
            importe="$ 1,450.80",
            descripcion="Compra con tarjeta terminación 1234",
            tipo=TipoTransaccion.EGRESO
        )
        self.assertEqual(tx.importe, 1450.80)
        self.assertEqual(tx.tipo, TipoTransaccion.EGRESO)

        # Test negative amount
        tx2 = Transaccion(
            fecha="2024-03-15",
            concepto="Cargo",
            importe="-500.25",
            descripcion="Retiro cajero",
            tipo=TipoTransaccion.EGRESO
        )
        self.assertEqual(tx2.importe, 500.25)

    def test_spanish_date_normalization(self):
        """Verifies diverse Spanish banking date representations."""
        test_cases = [
            ("15/01/2024", "2024-01-15"),
            ("15-01-2024", "2024-01-15"),
            ("15/01/24", "2024-01-15"),
            ("28 ENE 24", "2024-01-28"),
            ("03 DE MARZO DE 2025", "2025-03-03"),
            ("2024-10-06T12:00:00", "2024-10-06"),
            ("31-DIC-23", "2023-12-31"),
            ("12/SEP/2024", "2024-09-12"),
            ("05 AGO 2024", "2024-08-05"),
        ]
        for raw, expected in test_cases:
            normalized = normalize_spanish_date(raw)
            self.assertEqual(normalized, expected, f"Failed for raw date: {raw}")

    def test_deterministic_cfdi_xml_parsing(self):
        """Tests parsing of a Mexican CFDI 4.0 XML invoice."""
        cfdi_sample = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante 
    xmlns:cfdi="http://www.sat.gob.mx/cfd/4" 
    Version="4.0" 
    Folio="9876" 
    Fecha="2024-05-20T14:30:00" 
    SubTotal="2500.00" 
    Total="2900.00" 
    TipoDeComprobante="I" 
    Moneda="MXN">
    <cfdi:Emisor Rfc="CFE370814QI0" Nombre="COMISION FEDERAL DE ELECTRICIDAD"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="PUBLICO EN GENERAL"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="83101800" Cantidad="1" Descripcion="Consumo de energia electrica periodo mayo" ValorUnitario="2500.00" Importe="2500.00"/>
    </cfdi:Conceptos>
</cfdi:Comprobante>"""

        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(cfdi_sample)
            temp_path = f.name

        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertIsNone(raw_text)
            self.assertEqual(len(txs), 1)
            tx = txs[0]
            self.assertEqual(tx.fecha, "2024-05-20")
            self.assertEqual(tx.concepto, "COMISION FEDERAL DE ELECTRICIDAD")
            self.assertEqual(tx.importe, 2900.00)
            self.assertEqual(tx.tipo, TipoTransaccion.EGRESO)
            self.assertIn("Folio: 9876", tx.descripcion)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_cfdi_ecb_bank_statement_multi_level(self):
        """Verifies that CFDI with EstadoDeCuentaBancario extracts all child movements, not just first level."""
        cfdi_ecb = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante 
    xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
    xmlns:ecb="http://www.sat.gob.mx/ecb"
    xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
    Version="4.0" 
    Folio="1001" 
    Fecha="2024-06-01T10:00:00" 
    SubTotal="150.00" 
    Total="174.00" 
    TipoDeComprobante="I">
    <cfdi:Emisor Rfc="BBA830831LJ2" Nombre="BBVA MEXICO SA"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="CLIENTE EJEMPLO"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="84111506" Descripcion="Comision por manejo de cuenta" Importe="150.00"/>
    </cfdi:Conceptos>
    <cfdi:Complemento>
        <tfd:TimbreFiscalDigital UUID="AAAABBBB-CCCC-DDDD-EEEE-FFFF00001111"/>
        <ecb:EstadoDeCuentaBancario version="1.0" numeroCuenta="0123456789">
            <ecb:Movimientos>
                <ecb:Movimiento fecha="2024-05-02T09:15:00" descripcion="DEPOSITO NOMINA EMPRESA SA" importe="35000.00" tipo="DEPOSITO" referencia="REF001"/>
                <ecb:Movimiento fecha="2024-05-05T18:20:00" descripcion="COMPRA EN WALMART SUPER" importe="1450.50" tipo="CARGO" referencia="REF002"/>
                <ecb:Movimiento fecha="2024-05-10T12:00:00" descripcion="PAGO SERVICIO CFE" importe="620.00" tipo="CARGO" referencia="REF003"/>
            </ecb:Movimientos>
        </ecb:EstadoDeCuentaBancario>
    </cfdi:Complemento>
</cfdi:Comprobante>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(cfdi_ecb)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertIsNone(raw_text)
            self.assertEqual(len(txs), 3)
            # 1. Depósito Nómina (Ingreso)
            self.assertEqual(txs[0].fecha, "2024-05-02")
            self.assertEqual(txs[0].importe, 35000.00)
            self.assertEqual(txs[0].tipo, TipoTransaccion.INGRESO)
            # 2. Compra Walmart (Egreso)
            self.assertEqual(txs[1].fecha, "2024-05-05")
            self.assertEqual(txs[1].importe, 1450.50)
            self.assertEqual(txs[1].tipo, TipoTransaccion.EGRESO)
            # 3. Pago CFE (Egreso)
            self.assertEqual(txs[2].fecha, "2024-05-10")
            self.assertEqual(txs[2].importe, 620.00)
            self.assertEqual(txs[2].tipo, TipoTransaccion.EGRESO)
            self.assertIn("AAAABBBB-CCCC", txs[0].descripcion)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_cfdi_pagos_rep_complement(self):
        """Verifies that CFDI de Pagos (REP) extracts payment amounts and related invoice references."""
        cfdi_pagos = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante 
    xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
    xmlns:pago20="http://www.sat.gob.mx/Pagos20"
    xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
    Version="4.0" 
    Folio="PAG-55" 
    Fecha="2024-05-15T16:00:00" 
    SubTotal="0" 
    Total="0" 
    TipoDeComprobante="P">
    <cfdi:Emisor Rfc="PROV900101ABC" Nombre="PROVEEDOR INDUSTRIAL SA"/>
    <cfdi:Receptor Rfc="XAXX010101000" Nombre="MI EMPRESA SA"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="84111506" Descripcion="Pago" ValorUnitario="0" Importe="0"/>
    </cfdi:Conceptos>
    <cfdi:Complemento>
        <tfd:TimbreFiscalDigital UUID="99998888-7777-6666-5555-444433332222"/>
        <pago20:Pagos Version="2.0">
            <pago20:Pago FechaPago="2024-05-14T11:30:00" FormaDePagoP="03" MonedaP="MXN" Monto="18500.00" NumOperacion="SPEI-998877">
                <pago20:DoctoRelacionado IdDocumento="F1111111-2222-3333-4444-555555555555" Folio="120" ImpPagado="18500.00"/>
            </pago20:Pago>
        </pago20:Pagos>
    </cfdi:Complemento>
</cfdi:Comprobante>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(cfdi_pagos)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertIsNone(raw_text)
            self.assertEqual(len(txs), 1)
            self.assertEqual(txs[0].fecha, "2024-05-14")
            self.assertEqual(txs[0].importe, 18500.00)
            self.assertEqual(txs[0].tipo, TipoTransaccion.EGRESO)
            self.assertIn("F1111111", txs[0].descripcion)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_cfdi_addenda_hsbc_credit_statement(self):
        """Verifies CFDI with HSBC Addenda extracts all credit movements and ignores dummy $0.01 concept."""
        hsbc_credit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante 
    xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
    xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
    Version="4.0" 
    Folio="5470748606965402" 
    Fecha="2026-01-16T12:00:00" 
    SubTotal="0.01" 
    Total="0.01" 
    TipoDeComprobante="I">
    <cfdi:Emisor Rfc="HMI950125KG8" Nombre="HSBC MEXICO SA"/>
    <cfdi:Receptor Rfc="BERH900101XYZ" Nombre="HIMMLER EDUARDO BENITEZ ROMO"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="84121500" Descripcion="Servicios de Facturacion" Importe="0.01" ValorUnitario="0.01"/>
    </cfdi:Conceptos>
    <cfdi:Complemento>
        <tfd:TimbreFiscalDigital UUID="9BE9A901-C1C7-4C05-8B60-846E921F15DE"/>
    </cfdi:Complemento>
    <cfdi:Addenda>
        <DatosGenerales version="1.0" numerodecuenta="5470748606965402" nombredelCliente="HIMMLER EDUARDO BENITEZ ROMO" periodo="16 Dic 25 - 15 Ene 26">
            <Movimientos>
                <MovimientosDelCliente fecha="2025-12-16T12:00:00" descripcion="SU PAGO GRACIAS SPEI A CTA" importe="35000.00"/>
                <MovimientosDelCliente fecha="2025-12-17T12:00:00" descripcion="USO DE SALDO HSBC 2NOW" importe="1497.68"/>
                <MovimientosDelCliente fecha="2026-01-04T12:00:00" descripcion="GOOGLE Colab MOUNTAIN VIEW CA" importe="209.68"/>
                <MovimientoDelClienteFiscal fecha="2025-12-15T12:00:00" descripcion="CFE SUM SERV BAS CR MU MEX" RFCenajenante="CSS160330CP7" importe="755.00"/>
                <MovimientoDelClienteFiscal fecha="2025-12-15T12:00:00" descripcion="COSTCO HERMOSILLO HER" RFCenajenante="CME910715UB9" importe="1497.68"/>
            </Movimientos>
        </DatosGenerales>
    </cfdi:Addenda>
</cfdi:Comprobante>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(hsbc_credit_xml)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertIsNone(raw_text)
            self.assertEqual(len(txs), 5)
            # The dummy $0.01 concept must NOT be present
            self.assertFalse(any(t.concepto == "Servicios de Facturacion" for t in txs))
            # Payments to credit card are Ingreso
            self.assertEqual(txs[0].tipo, TipoTransaccion.INGRESO)
            self.assertEqual(txs[0].importe, 35000.00)
            self.assertEqual(txs[1].tipo, TipoTransaccion.INGRESO)
            self.assertEqual(txs[1].importe, 1497.68)
            # Charges/purchases are Egreso
            self.assertEqual(txs[2].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[2].importe, 209.68)
            self.assertEqual(txs[3].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[3].importe, 755.00)
            self.assertEqual(txs[4].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[4].importe, 1497.68)
            self.assertIn("CSS160330CP7", txs[3].descripcion)
            self.assertIn("5470748606965402", txs[3].descripcion)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_cfdi_addenda_hsbc_debit_statement(self):
        """Verifies CFDI with HSBC debit Addenda extracts payroll, scholarship, and charges."""
        hsbc_debit_xml = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante 
    xmlns:cfdi="http://www.sat.gob.mx/cfd/4"
    xmlns:tfd="http://www.sat.gob.mx/TimbreFiscalDigital"
    Version="4.0" 
    Folio="000006533808536" 
    Fecha="2026-01-31T12:00:00" 
    SubTotal="0.01" 
    Total="0.01" 
    TipoDeComprobante="I">
    <cfdi:Emisor Rfc="HMI950125KG8" Nombre="HSBC MEXICO SA"/>
    <cfdi:Receptor Rfc="BERH900101XYZ" Nombre="HIMMLER EDUARDO BENITEZ ROMO"/>
    <cfdi:Conceptos>
        <cfdi:Concepto ClaveProdServ="84121500" Descripcion="Servicios de Facturacion" Importe="0.01" ValorUnitario="0.01"/>
    </cfdi:Conceptos>
    <cfdi:Addenda>
        <DatosGenerales version="1.0" numerodecuenta="000006533808536" nombredelCliente="HIMMLER EDUARDO BENITEZ ROMO" periodo="01/01/2026-31/01/2026">
            <Movimientos>
                <MovimientosDelCliente fecha="2026-01-08T12:00:00" descripcion="PAGO DE BECA SECIHTI 2062265" importe="15477.57"/>
                <MovimientosDelCliente fecha="2026-01-09T12:00:00" descripcion="CGO Beca" importe="10477.58"/>
                <MovimientosDelCliente fecha="2026-01-14T12:00:00" descripcion="NOMINA M 1 01012026 AL 3101202 0025087" importe="28995.51"/>
                <MovimientosDelCliente fecha="2026-01-14T12:00:00" descripcion="PAGO DE TARJETA 5470688605226754 EN BPI" importe="13508.17"/>
            </Movimientos>
        </DatosGenerales>
    </cfdi:Addenda>
</cfdi:Comprobante>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(hsbc_debit_xml)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertIsNone(raw_text)
            self.assertEqual(len(txs), 4)
            # Beca deposit is Ingreso
            self.assertEqual(txs[0].tipo, TipoTransaccion.INGRESO)
            self.assertEqual(txs[0].importe, 15477.57)
            # Cargo Beca is Egreso
            self.assertEqual(txs[1].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[1].importe, 10477.58)
            # Nomina deposit is Ingreso
            self.assertEqual(txs[2].tipo, TipoTransaccion.INGRESO)
            self.assertEqual(txs[2].importe, 28995.51)
            # Pago de Tarjeta is Egreso
            self.assertEqual(txs[3].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[3].importe, 13508.17)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_parse_dummy_xml_files(self):
        """Verifies deterministic parsing directly with the XML fixtures in tests/fixtures/."""
        from tests.fixtures import get_fixture_path
        debito_path = get_fixture_path("dummy_xml_debito.xml")
        credito_path = get_fixture_path("dummy_xml_credito.xml")

        self.assertTrue(os.path.exists(debito_path), f"Missing {debito_path}")
        self.assertTrue(os.path.exists(credito_path), f"Missing {credito_path}")

        # 1. Debit test file
        txs_deb, raw_deb = parse_xml(debito_path)
        self.assertIsNotNone(txs_deb)
        self.assertIsNone(raw_deb)
        self.assertEqual(len(txs_deb), 3)
        self.assertEqual(txs_deb[0].importe, 1000.00)
        self.assertEqual(txs_deb[1].importe, 2000.00)
        self.assertEqual(txs_deb[2].importe, 3000.00)
        self.assertFalse(any(t.concepto == "Servicio de Prueba" for t in txs_deb))

        # 2. Credit test file
        txs_cred, raw_cred = parse_xml(credito_path)
        self.assertIsNotNone(txs_cred)
        self.assertIsNone(raw_cred)
        self.assertEqual(len(txs_cred), 5)
        self.assertFalse(any(t.concepto == "Servicio de Prueba" for t in txs_cred))

        # Payments to card are Ingreso
        self.assertEqual(txs_cred[0].tipo, TipoTransaccion.INGRESO)
        self.assertEqual(txs_cred[0].importe, 35000.00)
        self.assertEqual(txs_cred[1].tipo, TipoTransaccion.INGRESO)
        self.assertEqual(txs_cred[1].importe, 4000.00)

        # Fiscal merchant charges are Egreso
        self.assertEqual(txs_cred[2].tipo, TipoTransaccion.EGRESO)
        self.assertEqual(txs_cred[2].importe, 755.00)
        self.assertEqual(txs_cred[3].tipo, TipoTransaccion.EGRESO)
        self.assertEqual(txs_cred[3].importe, 1497.68)
        self.assertEqual(txs_cred[4].tipo, TipoTransaccion.EGRESO)
        self.assertEqual(txs_cred[4].importe, 1500.00)
        self.assertIn("RFC_TEST_001", txs_cred[2].descripcion)

    def test_ofx_banking_xml_parsing(self):
        """Verifies deterministic extraction of OFX bank transactions."""
        ofx_content = """<?xml version="1.0" encoding="UTF-8"?>
<OFX>
    <BANKMSGSRSV1>
        <STMTTRNRS>
            <STMTRS>
                <BANKTRANLIST>
                    <STMTTRN>
                        <TRNTYPE>DEBIT</TRNTYPE>
                        <DTPOSTED>20240410120000</DTPOSTED>
                        <TRNAMT>-850.00</TRNAMT>
                        <NAME>OXXO REVOLUCION</NAME>
                        <MEMO>Compra en tienda</MEMO>
                        <FITID>TXN001</FITID>
                    </STMTTRN>
                    <STMTTRN>
                        <TRNTYPE>CREDIT</TRNTYPE>
                        <DTPOSTED>20240415120000</DTPOSTED>
                        <TRNAMT>15000.00</TRNAMT>
                        <NAME>TRANSFERENCIA RECIBIDA</NAME>
                        <MEMO>SPEI Cliente</MEMO>
                        <FITID>TXN002</FITID>
                    </STMTTRN>
                </BANKTRANLIST>
            </STMTRS>
        </STMTTRNRS>
    </BANKMSGSRSV1>
</OFX>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(ofx_content)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertEqual(len(txs), 2)
            self.assertEqual(txs[0].fecha, "2024-04-10")
            self.assertEqual(txs[0].importe, 850.00)
            self.assertEqual(txs[0].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[1].fecha, "2024-04-15")
            self.assertEqual(txs[1].importe, 15000.00)
            self.assertEqual(txs[1].tipo, TipoTransaccion.INGRESO)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def test_generic_banking_xml_parsing(self):
        """Verifies deterministic parsing of generic XML with repeating movement nodes."""
        generic_xml = """<?xml version="1.0" encoding="UTF-8"?>
<estado_cuenta banco="Banamex" cuenta="12345678">
    <movimientos>
        <movimiento>
            <fecha>15/05/2024</fecha>
            <descripcion>PAGO INTERNET TELMEX</descripcion>
            <cargo>599.00</cargo>
            <abono>0.00</abono>
            <referencia>TLM-123</referencia>
        </movimiento>
        <movimiento>
            <fecha>16/05/2024</fecha>
            <descripcion>DEPOSITO EN VENTANILLA</descripcion>
            <cargo>0.00</cargo>
            <abono>2500.00</abono>
            <referencia>DEP-456</referencia>
        </movimiento>
    </movimientos>
</estado_cuenta>"""
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as f:
            f.write(generic_xml)
            temp_path = f.name
        try:
            txs, raw_text = parse_xml(temp_path)
            self.assertIsNotNone(txs)
            self.assertEqual(len(txs), 2)
            self.assertEqual(txs[0].fecha, "2024-05-15")
            self.assertEqual(txs[0].importe, 599.00)
            self.assertEqual(txs[0].tipo, TipoTransaccion.EGRESO)
            self.assertEqual(txs[1].fecha, "2024-05-16")
            self.assertEqual(txs[1].importe, 2500.00)
            self.assertEqual(txs[1].tipo, TipoTransaccion.INGRESO)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


    def test_excel_and_tsv_export(self):
        """Verifies multi-sheet Excel generation and TSV formatting."""
        tx1 = Transaccion(fecha="2024-01-10", concepto="Cliente A", importe=5000.0, descripcion="Factura 1", tipo=TipoTransaccion.INGRESO)
        tx2 = Transaccion(fecha="2024-01-12", concepto="Telmex", importe=799.0, descripcion="Internet", tipo=TipoTransaccion.EGRESO)
        
        df = create_standard_dataframe([tx1, tx2])
        df_ingresos = df[df["tipo"] == "ingreso"]
        df_egresos = df[df["tipo"] == "egreso"]

        # 1. Test TSV format for direct Excel paste
        tsv_egresos = df_to_tsv(df_egresos)
        self.assertIn("fecha\tconcepto\timporte\tdescripción", tsv_egresos)
        self.assertIn("2024-01-12\tTelmex\t799.0\tInternet", tsv_egresos)
        self.assertNotIn("tipo", tsv_egresos)

        # 2. Test multi-sheet Excel bytes
        excel_bytes = df_to_excel_bytes(df_ingresos, df_egresos)
        self.assertGreater(len(excel_bytes), 100)

        # Re-read Excel bytes to check sheets
        import io
        xls = pd.ExcelFile(io.BytesIO(excel_bytes))
        self.assertIn("Ingresos", xls.sheet_names)
        self.assertIn("Egresos", xls.sheet_names)

        df_in_read = pd.read_excel(xls, sheet_name="Ingresos")
        df_eg_read = pd.read_excel(xls, sheet_name="Egresos")
        self.assertEqual(len(df_in_read), 1)
        self.assertEqual(len(df_eg_read), 1)
        self.assertEqual(df_in_read.iloc[0]["importe"], 5000.0)
        self.assertEqual(df_eg_read.iloc[0]["importe"], 799.0)

    def test_ollama_pydantic_schema_generation(self):
        """Verifies that the Pydantic JSON schema conforms to Ollama's format requirements."""
        schema = TransaccionesResponse.model_json_schema()
        self.assertEqual(schema.get("type"), "object")
        self.assertIn("transacciones", schema.get("properties", {}))
        self.assertIn("properties", schema)


    def test_clean_and_parse_llm_json(self):
        """Verifies robust handling of empty LLM responses, thinking tokens, and markdown blocks."""
        from llm_client import clean_and_parse_llm_json

        # 1. Empty string must return empty list (prevents 'Expecting value: line 1 column 1' crash)
        self.assertEqual(clean_and_parse_llm_json(""), [])
        self.assertEqual(clean_and_parse_llm_json("   \n\t  "), [])

        # 2. Markdown fenced block
        md_json = '```json\n{"transacciones": [{"fecha": "2024-01-15", "concepto": "CFE", "importe": 450.0, "descripcion": "Luz", "tipo": "egreso"}]}\n```'
        res_md = clean_and_parse_llm_json(md_json)
        self.assertEqual(len(res_md), 1)
        self.assertEqual(res_md[0].concepto, "CFE")

        # 3. Thinking tags in content
        think_json = '<think>Analizando transacciones...</think>{"transacciones": [{"fecha": "2024-02-10", "concepto": "Nomina", "importe": 15000.0, "descripcion": "Pago", "tipo": "ingreso"}]}'
        res_think = clean_and_parse_llm_json(think_json)
        self.assertEqual(len(res_think), 1)
        self.assertEqual(res_think[0].concepto, "Nomina")

        # 4. JSON inside thinking field when content is empty
        res_param = clean_and_parse_llm_json("", thinking_content='Thinking... {"transacciones": [{"fecha": "2024-03-01", "concepto": "Super", "importe": 800.0, "descripcion": "Despensa", "tipo": "egreso"}]}')
        self.assertEqual(len(res_param), 1)
        self.assertEqual(res_param[0].concepto, "Super")

    def test_compact_markdown_tables(self):
        """Verifies that whitespace padding from Docling TableFormer is collapsed cleanly."""
        from llm_client import compact_markdown_tables

        bloated_table = (
            "| Fecha                    | Concepto                  | Importe         |\n"
            "| ------------------------ | ------------------------- | --------------- |\n"
            "| 15-Ene-2024              | Amazon Mexico             | $ 1,250.00      |"
        )
        compact = compact_markdown_tables(bloated_table)
        self.assertIn("| Fecha | Concepto | Importe |", compact)
        self.assertIn("| 15-Ene-2024 | Amazon Mexico | $ 1,250.00 |", compact)
        self.assertLess(len(compact), len(bloated_table))

    def test_chunk_filter_and_table_row_detection(self):
        """Verifies candidate chunk filtering and row classification."""
        from llm_client import is_table_data_row, chunk_has_transaction_rows

        header_row = "| Fecha de la operación | Concepto | Monto |"
        sep_row = "| --- | --- | --- |"
        data_row = "| 12-Dic-2025 | Caffenio | $ 46.00 |"
        disclaimer_text = "Consulte términos y condiciones en www.santander.com.mx. Comisión por pago tardío 400 MXN."

        self.assertFalse(is_table_data_row(header_row))
        self.assertFalse(is_table_data_row(sep_row))
        self.assertTrue(is_table_data_row(data_row))

        self.assertTrue(chunk_has_transaction_rows(data_row))
        self.assertFalse(chunk_has_transaction_rows(disclaimer_text))

    def test_table_aware_chunking_preserves_headers(self):
        """Verifies that split table chunks inherit table headers so LLM maintains schema context."""
        from llm_client import chunk_text

        table_lines = [
            "| CARGOS Y ABONOS | CARGOS Y ABONOS | CARGOS Y ABONOS |",
            "| --- | --- | --- |",
            "| Fecha | Concepto | Monto |",
        ]
        # Generate 12 data rows
        for i in range(12):
            table_lines.append(f"| {i+1}-Ene-2025 | Comercio {i+1} | $ {100*(i+1)}.00 |")

        table_text = "\n".join(table_lines)
        chunks = chunk_text(table_text, max_chunk_chars=1800, max_table_rows=8)
        self.assertGreaterEqual(len(chunks), 2)

        # Both chunks must preserve the header lines
        for chunk in chunks:
            self.assertIn("Fecha | Concepto | Monto", chunk)

    def test_individual_file_configs_routing(self):
        """Verifies that individual file configs route correctly between deterministic, hybrid, and PDF modes."""
        from tests.fixtures import get_fixture_path
        debito_path = get_fixture_path("dummy_xml_debito.xml")

        # Test simulated file_configs dictionary
        file_configs = {
            "dummy_xml_debito.xml": {
                "type": "xml",
                "strategy_label": "⚡ Determinista",
                "xml_mode": "deterministic",
                "user_rfc": "XYZ987654321",
                "model_name": "llama3.2"
            },
            "sample_bank.pdf": {
                "type": "pdf",
                "strategy_label": "⚡ Digital Nativo",
                "enable_ocr": False,
                "accurate_tables": False,
                "ocr_scale": 2.0,
                "model_name": "qwen2.5",
                "page_range": (1, 3)
            }
        }

        cfg_xml = file_configs["dummy_xml_debito.xml"]
        self.assertEqual(cfg_xml["xml_mode"], "deterministic")
        self.assertEqual(cfg_xml["user_rfc"], "XYZ987654321")

        # When parsed with this RFC (Receptor), movements are classified properly
        txs, _ = parse_xml(debito_path, user_rfc=cfg_xml["user_rfc"])
        self.assertEqual(len(txs), 3)

        cfg_pdf = file_configs["sample_bank.pdf"]
        self.assertFalse(cfg_pdf["enable_ocr"])
        self.assertEqual(cfg_pdf["page_range"], (1, 3))
        self.assertEqual(cfg_pdf["model_name"], "qwen2.5")

    def test_general_strategy_defaults_and_inheritance(self):
        """Verifies general strategy defaults (Máxima Precisión by default) and per-file inheritance/override."""
        from parsers import resolve_pdf_strategy, resolve_xml_strategy

        # 1. Defaults are Máxima Precisión
        default_pdf = resolve_pdf_strategy("🌐 Usar Estrategia General")
        self.assertEqual(default_pdf["strategy_label"], "🎯 Máxima Precisión")
        self.assertTrue(default_pdf["enable_ocr"])
        self.assertTrue(default_pdf["accurate_tables"])
        self.assertEqual(default_pdf["ocr_scale"], 2.5)

        default_xml = resolve_xml_strategy("🌐 Usar Estrategia General")
        self.assertEqual(default_xml["strategy_label"], "🎯 Máxima Precisión")
        self.assertEqual(default_xml["xml_mode"], "hybrid")

        # 2. General strategy updates propagate to inherited files
        gen_pdf_digital = "⚡ Digital Nativo (10x Rápido - Omite OCR)"
        inherited_pdf = resolve_pdf_strategy("🌐 Usar Estrategia General", general_strategy=gen_pdf_digital)
        self.assertEqual(inherited_pdf["strategy_label"], "⚡ Digital Nativo")
        self.assertFalse(inherited_pdf["enable_ocr"])
        self.assertFalse(inherited_pdf["accurate_tables"])
        self.assertEqual(inherited_pdf["ocr_scale"], 2.0)

        gen_xml_det = "⚡ Determinista Estricto (CFDI, Addenda, OFX, CAMT - 0s latencia)"
        inherited_xml = resolve_xml_strategy("🌐 Usar Estrategia General", general_strategy=gen_xml_det)
        self.assertEqual(inherited_xml["strategy_label"], "⚡ Determinista Estricto")
        self.assertEqual(inherited_xml["xml_mode"], "deterministic")

        # 3. Explicit per-file override takes precedence over general strategy
        override_pdf = resolve_pdf_strategy("🔍 Escaneo Físico", general_strategy=gen_pdf_digital)
        self.assertEqual(override_pdf["strategy_label"], "🔍 Escaneo Físico")
        self.assertTrue(override_pdf["enable_ocr"])
        self.assertFalse(override_pdf["accurate_tables"])

        override_xml = resolve_xml_strategy("🤖 Forzar Ollama LLM", general_strategy=gen_xml_det)
        self.assertEqual(override_xml["strategy_label"], "🤖 Forzar Ollama LLM")
        self.assertEqual(override_xml["xml_mode"], "llm")

    def test_arithmetic_reconciliation_debit_and_credit(self):
        """Verifies reconcile_transactions accurately reconciles both debit and credit card statements and flags discrepancies."""
        # 1. Debit account: Saldo Inicial 1000 + Ingreso 2500 - Egreso 500 = Saldo Final 3000
        tx_ingreso = Transaccion(fecha="2024-01-10", concepto="Abono Nómina", importe=2500.0, descripcion="Transferencia nómina", tipo=TipoTransaccion.INGRESO)
        tx_egreso = Transaccion(fecha="2024-01-12", concepto="Retiro Cajero", importe=500.0, descripcion="Retiro efectivo", tipo=TipoTransaccion.EGRESO)

        res_debito = reconcile_transactions([tx_ingreso, tx_egreso], saldo_inicial=1000.0, saldo_final=3000.0)
        self.assertTrue(res_debito.conciliado)
        self.assertEqual(res_debito.modo_calculo, "debito")
        self.assertEqual(res_debito.total_ingresos, 2500.0)
        self.assertEqual(res_debito.total_egresos, 500.0)
        self.assertEqual(res_debito.saldo_final_calculado, 3000.0)
        self.assertEqual(res_debito.diferencia, 0.0)

        # 2. Credit card debt account: Saldo Inicial 5000 - Pago/Ingreso 2000 + Compras/Egreso 800 = Saldo Final 3800
        tx_pago = Transaccion(fecha="2024-01-05", concepto="Pago Tarjeta", importe=2000.0, descripcion="Pago en ventanilla", tipo=TipoTransaccion.INGRESO)
        tx_compra = Transaccion(fecha="2024-01-15", concepto="Compra Tienda", importe=800.0, descripcion="Supermercado", tipo=TipoTransaccion.EGRESO)

        res_credito = reconcile_transactions([tx_pago, tx_compra], saldo_inicial=5000.0, saldo_final=3800.0)
        self.assertTrue(res_credito.conciliado)
        self.assertEqual(res_credito.modo_calculo, "credito")
        self.assertEqual(res_credito.total_ingresos, 2000.0)
        self.assertEqual(res_credito.total_egresos, 800.0)
        self.assertEqual(res_credito.saldo_final_calculado, 3800.0)
        self.assertEqual(res_credito.diferencia, 0.0)

        # 3. Discrepancy detection: Missing a transaction
        res_discrepancia = reconcile_transactions([tx_ingreso], saldo_inicial=1000.0, saldo_final=3000.0)
        self.assertFalse(res_discrepancia.conciliado)
        self.assertGreater(res_discrepancia.diferencia, 0.05)

        # 4. Incomplete statement balances
        res_nd = reconcile_transactions([tx_ingreso], saldo_inicial=None, saldo_final=3000.0)
        self.assertIsNone(res_nd.conciliado)
        self.assertEqual(res_nd.modo_calculo, "no_disponible")

    def test_extract_statement_balances_regex(self):
        """Verifies extract_statement_balances extracts saldo_inicial and saldo_final from text/markdown blocks."""
        sample_markdown = """
        # ESTADO DE CUENTA BANCARIO - BANCO NACIONAL
        Periodo: Del 01/01/2024 al 31/01/2024
        
        | Concepto Resumen | Monto |
        |---|---|
        | Saldo Anterior | $ 12,450.50 |
        | Total de Depósitos / Abonos | $ 5,000.00 |
        | Total de Retiros / Cargos | $ 3,200.00 |
        | Saldo Actual al Corte | $ 14,250.50 |
        """
        balances = extract_statement_balances(sample_markdown)
        self.assertEqual(balances.get("saldo_inicial"), 12450.50)
        self.assertEqual(balances.get("saldo_final"), 14250.50)

        # Alternate phrasing in text
        text_phrasing = "Resumen del periodo: Saldo inicial: $1,500.00. Nuevo saldo al corte: $2,800.75"
        balances_alt = extract_statement_balances(text_phrasing)
        self.assertEqual(balances_alt.get("saldo_inicial"), 1500.00)
        self.assertEqual(balances_alt.get("saldo_final"), 2800.75)

    def test_extract_statement_balances_negative_and_credit_card(self):
        """Verifies extraction of overdraft balances, accounting suffixes, multiline, and credit card headers."""
        # 1. Overdraft with minus sign and parentheses
        text_neg = "Saldo Inicial: -$1,500.00 | Saldo Final: ($2,350.25)"
        bal_neg = extract_statement_balances(text_neg)
        self.assertEqual(bal_neg.get("saldo_inicial"), -1500.00)
        self.assertEqual(bal_neg.get("saldo_final"), -2350.25)

        # 2. Accounting suffix (DR = debit/negative, CR = credit/positive)
        text_dr = "Saldo Anterior: 4,200.50 DR \n Nuevo Saldo: 1,800.00 CR"
        bal_dr = extract_statement_balances(text_dr)
        self.assertEqual(bal_dr.get("saldo_inicial"), -4200.50)
        self.assertEqual(bal_dr.get("saldo_final"), 1800.00)

        # 3. Multiline extraction where amount appears on next line
        text_multiline = "Saldo Inicial\n$1,500.00\nSaldo al Corte\n$3,750.50"
        bal_multi = extract_statement_balances(text_multiline)
        self.assertEqual(bal_multi.get("saldo_inicial"), 1500.00)
        self.assertEqual(bal_multi.get("saldo_final"), 3750.50)

        # 4. Credit card specific statement headers
        text_cc = """
        ESTADO DE CUENTA TARJETA DE CRÉDITO
        Saldo Anterior: $ 5,400.00
        Total Cargos: $ 3,100.00
        Total Abonos: $ 2,500.00
        Saldo Deudor Total: $ 6,000.00
        Nuevo Saldo: $ 6,000.00
        """
        bal_cc = extract_statement_balances(text_cc)
        self.assertEqual(bal_cc.get("saldo_anterior"), 5400.00)
        self.assertEqual(bal_cc.get("saldo_inicial"), 5400.00)
        self.assertEqual(bal_cc.get("total_cargos"), 3100.00)
        self.assertEqual(bal_cc.get("total_abonos"), 2500.00)
        self.assertEqual(bal_cc.get("saldo_deudor_total"), 6000.00)
        self.assertEqual(bal_cc.get("saldo_final"), 6000.00)

    def test_infer_statement_mode(self):
        """Verifies deterministic inference of accounting mode ('debito', 'credito', 'ambiguo')."""
        # Debit signals
        text_deb = "Estado de cuenta de cheques. CLABE interbancaria: 012180001234567890. Saldo disponible al corte."
        self.assertEqual(infer_statement_mode(text_deb), "debito")

        # Credit card signals
        text_cred = "Tarjeta de crédito Oro. Límite de crédito: $80,000.00. Pago para no generar intereses: $4,500.00."
        self.assertEqual(infer_statement_mode(text_cred), "credito")

        # Mathematical verification when text is neutral but summary_data fits debit
        summary_deb = {
            "saldo_inicial": 1000.0,
            "saldo_final": 1800.0,
            "total_abonos": 1000.0,
            "total_cargos": 200.0,
        }
        self.assertEqual(infer_statement_mode("Resumen de movimientos", summary_deb), "debito")

        # Mathematical verification when text is neutral but summary_data fits credit
        summary_cred = {
            "saldo_inicial": 5000.0,
            "saldo_final": 4200.0,
            "total_abonos": 1000.0,
            "total_cargos": 200.0,
        }
        self.assertEqual(infer_statement_mode("Resumen de movimientos", summary_cred), "credito")

        # Ambiguous when signals are missing or conflicting
        self.assertEqual(infer_statement_mode("Documento bancario general sin detalles"), "ambiguo")

    def test_inverted_cashflow_rejected_never_switches_to_credit(self):
        """
        Critical security test: verifies that a statement in debit mode where transactions
        have inverted INGRESO/EGRESO types fails reconciliation with cuadra=False and NEVER
        silently flips to credit mode to claim false reconciliation.
        """
        # Real situation: opening balance 10,000, closing 12,000.
        # Genuine flow: INGRESO 3,000, EGRESO 1,000 (10000 + 3000 - 1000 = 12000).
        # Inverted flow (e.g. LLM misclassification): INGRESO 1,000, EGRESO 3,000.
        # In debit: 10000 + 1000 - 3000 = 8000 != 12000 (diff = 4000).
        # In credit: 10000 - 1000 + 3000 = 12000 (diff = 0 -> FALSE POSITIVE IN PREVIOUS REPO!).
        tx_inverted_1 = Transaccion(fecha="2024-05-10", concepto="Abono", importe=1000.0, descripcion="Deposito", tipo=TipoTransaccion.INGRESO)
        tx_inverted_2 = Transaccion(fecha="2024-05-12", concepto="Cargo", importe=3000.0, descripcion="Retiro", tipo=TipoTransaccion.EGRESO)

        debit_text = "Estado de cuenta Bancario. Cuenta de cheques. CLABE 012180000000000000."
        res = reconcile_transactions(
            [tx_inverted_1, tx_inverted_2],
            saldo_inicial=10000.0,
            saldo_final=12000.0,
            text=debit_text
        )

        self.assertEqual(res.modo_contable, "debito")
        self.assertFalse(res.cuadra)
        self.assertFalse(res.conciliado)
        self.assertEqual(res.diferencia, 4000.0)
        self.assertNotEqual(res.modo_calculo, "credito")

    def test_reconciliation_ambiguous_mode_never_reconciles(self):
        """Verifies that an ambiguous statement mode forces cuadra=False even if arithmetic could match."""
        tx1 = Transaccion(fecha="2024-05-10", concepto="Tx1", importe=500.0, descripcion="Desc", tipo=TipoTransaccion.INGRESO)
        res = reconcile_transactions(
            [tx1],
            saldo_inicial=1000.0,
            saldo_final=1500.0,
            modo_contable="ambiguo"
        )
        self.assertFalse(res.cuadra)
        self.assertFalse(res.conciliado)
        self.assertEqual(res.modo_contable, "ambiguo")

    def test_running_balance_chain_compliance(self):
        """Verifies calculation of running balance chain compliance percentage."""
        # 1. 100% compliant chain:
        # Initial: 1000.0
        # + Ingreso 500 -> saldo_renglon 1500.0 (OK)
        # - Egreso 200 -> saldo_renglon 1300.0 (OK)
        # - Egreso 300 -> saldo_renglon 1000.0 (OK)
        tx1 = Transaccion(fecha="2024-06-01", concepto="Abono", importe=500.0, descripcion="A", tipo=TipoTransaccion.INGRESO, saldo_renglon=1500.0)
        tx2 = Transaccion(fecha="2024-06-02", concepto="Cargo", importe=200.0, descripcion="B", tipo=TipoTransaccion.EGRESO, saldo_renglon=1300.0)
        tx3 = Transaccion(fecha="2024-06-03", concepto="Cargo", importe=300.0, descripcion="C", tipo=TipoTransaccion.EGRESO, saldo_renglon=1000.0)

        res = reconcile_transactions([tx1, tx2, tx3], saldo_inicial=1000.0, saldo_final=1000.0, modo_contable="debito")
        self.assertTrue(res.cuadra)
        self.assertEqual(res.filas_con_saldo, 3)
        self.assertEqual(res.filas_cumplen_cadena, 3)
        self.assertEqual(res.cumplimiento_cadena_saldo, 100.0)

        # 2. Broken chain in one row:
        # tx3 has corrupt saldo_renglon 800.0 instead of 1000.0
        tx3_broken = Transaccion(fecha="2024-06-03", concepto="Cargo", importe=300.0, descripcion="C", tipo=TipoTransaccion.EGRESO, saldo_renglon=800.0)
        res_broken = reconcile_transactions([tx1, tx2, tx3_broken], saldo_inicial=1000.0, saldo_final=1000.0, modo_contable="debito")
        self.assertTrue(res_broken.cuadra)
        self.assertEqual(res_broken.filas_con_saldo, 3)
        self.assertEqual(res_broken.filas_cumplen_cadena, 2)
        self.assertAlmostEqual(res_broken.cumplimiento_cadena_saldo, 66.67, places=1)

    def test_clasificar_estado_documento(self):
        """Verifies document classification into 'VERIFICADO', 'REVISAR', and 'RECHAZADO'."""
        # 1. VERIFICADO: cuadra, debito, 100% cobertura, 0 rechazos
        st1 = clasificar_estado_documento(cuadra=True, modo_contable="debito", cobertura=1.0, filas_rechazadas_count=0, tiene_saldos=True)
        self.assertEqual(st1, "VERIFICADO")

        # 2. REVISAR: cuadra but cobertura < 0.99
        st2 = clasificar_estado_documento(cuadra=True, modo_contable="debito", cobertura=0.95, filas_rechazadas_count=0, tiene_saldos=True)
        self.assertEqual(st2, "REVISAR")

        # 3. REVISAR: cuadra but has rejected rows
        st3 = clasificar_estado_documento(cuadra=True, modo_contable="credito", cobertura=1.0, filas_rechazadas_count=2, tiene_saldos=True)
        self.assertEqual(st3, "REVISAR")

        # 4. REVISAR: cuadra but no balances extracted from document
        st4 = clasificar_estado_documento(cuadra=True, modo_contable="debito", cobertura=1.0, filas_rechazadas_count=0, tiene_saldos=False)
        self.assertEqual(st4, "REVISAR")

        # 5. RECHAZADO: does not balance
        st5 = clasificar_estado_documento(cuadra=False, modo_contable="debito", cobertura=1.0, filas_rechazadas_count=0, tiene_saldos=True)
        self.assertEqual(st5, "RECHAZADO")

        # 6. RECHAZADO: ambiguous accounting mode
        st6 = clasificar_estado_documento(cuadra=True, modo_contable="ambiguo", cobertura=1.0, filas_rechazadas_count=0, tiene_saldos=True)
        self.assertEqual(st6, "RECHAZADO")

    def test_evaluate_against_golden_unit(self):
        """
        Unit tests for evaluate_against_golden:
        - Perfect extraction achieves 1.0 in precision, recall, F1, exactitud_tipo and falso_verificado == False.
        - Inverted cashflow type is detected with exactitud_tipo < 1.0 and marks falso_verificado == True.
        - Missing row causes recall < 1.0 and marks falso_verificado == True.
        - Spurious row causes precision < 1.0 and marks falso_verificado == True.
        """
        golden = GoldenCaseExpected(
            case_id="unit_test_case",
            banco="Test Bank",
            tipo_documento="pdf_digital",
            modo_esperado="debito",
            saldo_inicial=1000.0,
            saldo_final=1900.0,
            total_cargos=100.0,
            total_abonos=1000.0,
            movimientos_esperados=[
                Transaccion(fecha="2024-01-01", concepto="Abono", importe=1000.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO),
                Transaccion(fecha="2024-01-02", concepto="Cargo", importe=100.0, descripcion="Cargo", tipo=TipoTransaccion.EGRESO),
            ]
        )

        # 1. Perfect match
        extracted_perfect = [
            Transaccion(fecha="2024-01-01", concepto="Abono", importe=1000.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO),
            Transaccion(fecha="2024-01-02", concepto="Cargo", importe=100.0, descripcion="Cargo", tipo=TipoTransaccion.EGRESO),
        ]
        res_perf = evaluate_against_golden({"transacciones": extracted_perfect, "estado": "VERIFICADO"}, golden)
        self.assertEqual(res_perf["precision_filas"], 1.0)
        self.assertEqual(res_perf["recall_filas"], 1.0)
        self.assertEqual(res_perf["f1_filas"], 1.0)
        self.assertEqual(res_perf["exactitud_tipo"], 1.0)
        self.assertFalse(res_perf["falso_verificado"])

        # 2. Inverted cashflow type on cargo (classified as ingreso)
        extracted_inverted = [
            Transaccion(fecha="2024-01-01", concepto="Abono", importe=1000.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO),
            Transaccion(fecha="2024-01-02", concepto="Cargo", importe=100.0, descripcion="Cargo", tipo=TipoTransaccion.INGRESO),
        ]
        res_inv = evaluate_against_golden({"transacciones": extracted_inverted, "estado": "VERIFICADO"}, golden)
        self.assertEqual(res_inv["exactitud_tipo"], 0.5)
        self.assertTrue(res_inv["falso_verificado"], "Inverted cashflow must trigger falso_verificado if marked VERIFICADO")

        # 3. Missing row (missing cargo)
        extracted_missing = [
            Transaccion(fecha="2024-01-01", concepto="Abono", importe=1000.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO),
        ]
        res_miss = evaluate_against_golden({"transacciones": extracted_missing, "estado": "VERIFICADO"}, golden)
        self.assertLess(res_miss["recall_filas"], 1.0)
        self.assertTrue(res_miss["falso_verificado"])

        # 4. Spurious extra row
        extracted_spurious = [
            Transaccion(fecha="2024-01-01", concepto="Abono", importe=1000.0, descripcion="Abono", tipo=TipoTransaccion.INGRESO),
            Transaccion(fecha="2024-01-02", concepto="Cargo", importe=100.0, descripcion="Cargo", tipo=TipoTransaccion.EGRESO),
            Transaccion(fecha="2024-01-03", concepto="Fantasma", importe=50.0, descripcion="Fantasma", tipo=TipoTransaccion.EGRESO),
        ]
        res_spur = evaluate_against_golden({"transacciones": extracted_spurious, "estado": "VERIFICADO"}, golden)
        self.assertLess(res_spur["precision_filas"], 1.0)
        self.assertTrue(res_spur["falso_verificado"])

    def test_golden_dataset_regression(self):
        """
        Continuous Quality Gate across the Golden Dataset.
        Iterates over all cases in tests/fixtures/golden_dataset/, extracts transactions,
        validates the running balance chain, checks arithmetic reconciliation, and asserts:
          - Tasa de falso_verificado == 0 (zero false-verified documents)
          - f1_filas >= 0.99
          - exactitud_tipo == 1.0
        """
        golden_dir = os.path.join(os.path.dirname(__file__), "tests", "fixtures", "golden_dataset")
        self.assertTrue(os.path.exists(golden_dir), f"Golden dataset directory not found: {golden_dir}")

        case_dirs = [
            d for d in os.listdir(golden_dir)
            if os.path.isdir(os.path.join(golden_dir, d)) and not d.startswith(".")
        ]
        self.assertGreaterEqual(len(case_dirs), 4, f"Expected at least 4 golden cases, found: {case_dirs}")

        evaluation_results = []

        for c_dir in sorted(case_dirs):
            c_path = os.path.join(golden_dir, c_dir)
            exp_file = os.path.join(c_path, "expected.json")
            self.assertTrue(os.path.exists(exp_file), f"expected.json missing in {c_path}")

            with open(exp_file, "r", encoding="utf-8") as f:
                exp_data = json.load(f)
            golden = GoldenCaseExpected(**exp_data)

            doc_file = None
            for fname in os.listdir(c_path):
                if fname.startswith("documento"):
                    doc_file = os.path.join(c_path, fname)
                    break
            self.assertIsNotNone(doc_file, f"Document file missing in {c_path}")

            with open(doc_file, "r", encoding="utf-8") as f:
                doc_text = f.read()

            if golden.tipo_documento == "xml":
                txs, _ = parse_xml(doc_file)
            else:
                txs, unparsed, meta = parse_docling_markdown_tables(
                    doc_text,
                    modo_contable=golden.modo_esperado
                )

            # Validate running balance chain
            txs_val, chain_meta = validate_running_balance_chain(
                txs,
                saldo_inicial=golden.saldo_inicial,
                modo_contable=golden.modo_esperado
            )

            # Reconcile transactions
            recon = reconcile_transactions(
                txs_val,
                saldo_inicial=golden.saldo_inicial,
                saldo_final=golden.saldo_final,
                modo_contable=golden.modo_esperado,
                text=doc_text
            )

            baja_conf = sum(1 for t in txs_val if getattr(t, "confianza", 1.0) < 0.75)
            estado = clasificar_estado_documento(
                cuadra=recon.cuadra,
                modo_contable=golden.modo_esperado,
                cobertura=1.0,
                filas_rechazadas_count=0,
                tiene_saldos=True,
                filas_baja_confianza_count=baja_conf
            )

            res = {
                "transacciones": txs_val,
                "estado": estado,
                "reconciliacion": recon
            }

            metrics = evaluate_against_golden(res, golden)
            evaluation_results.append(metrics)

            # Assert strict quality gates
            self.assertFalse(
                metrics["falso_verificado"],
                f"Falso verificado detected for {golden.case_id}: F1={metrics['f1_filas']}, exactitud_tipo={metrics['exactitud_tipo']}"
            )
            self.assertGreaterEqual(
                metrics["f1_filas"],
                0.99,
                f"F1 score below 0.99 for {golden.case_id}: {metrics['f1_filas']}"
            )
            self.assertEqual(
                metrics["exactitud_tipo"],
                1.0,
                f"Exactitud de tipo below 1.0 for {golden.case_id}: {metrics['exactitud_tipo']}"
            )
            self.assertTrue(
                recon.cuadra,
                f"Balance does not reconcile for {golden.case_id}: diff={recon.diferencia}"
            )


def evaluate_against_golden(
    extracted_result: Any,
    golden_expected: GoldenCaseExpected
) -> Dict[str, Any]:
    """
    Evalúa el resultado de extracción contra la verdad absoluta (GoldenCaseExpected).
    Empareja movimientos extraídos vs esperados usando la clave compuesta (fecha_iso, round(importe, 2), tipo).

    Métricas calculadas:
      - precision_filas = TP / max(TP + FP, 1)
      - recall_filas = TP / max(TP + FN, 1)
      - f1_filas = 2 * (precision * recall) / max(precision + recall, 1e-9)
      - exactitud_tipo: porcentaje de filas emparejadas por (fecha_iso, round(importe, 2)) cuyo tipo coincide exactamente.
      - falso_verificado: bool: True si el sistema marcó el documento como 'VERIFICADO' pero f1_filas < 1.0 o exactitud_tipo < 1.0.
    """
    if isinstance(extracted_result, dict):
        extracted_txs = extracted_result.get("transacciones", [])
        estado = str(extracted_result.get("estado", "")).upper()
    elif hasattr(extracted_result, "transacciones"):
        extracted_txs = getattr(extracted_result, "transacciones", [])
        estado = str(getattr(extracted_result, "estado", "")).upper()
    elif isinstance(extracted_result, list):
        extracted_txs = extracted_result
        estado = "VERIFICADO"
    else:
        extracted_txs = []
        estado = ""

    expected_txs = list(golden_expected.movimientos_esperados)

    def normalize_tx_tuple(tx: Any) -> Tuple[str, float, str]:
        f = getattr(tx, "fecha", "")
        f_iso = normalize_spanish_date(f) if f else ""
        imp = round(abs(float(getattr(tx, "importe", 0.0))), 2)
        t = getattr(tx, "tipo", "")
        t_val = t.value if hasattr(t, "value") else str(t)
        t_clean = "ingreso" if str(t_val).lower() in ("ingreso", "abono") else "egreso"
        return (f_iso, imp, t_clean)

    ext_norm = [normalize_tx_tuple(tx) for tx in extracted_txs]
    exp_norm = [normalize_tx_tuple(tx) for tx in expected_txs]

    # 1. Match candidates by (fecha_iso, importe) to evaluate exactitud_tipo
    unmatched_exp_for_amount = list(exp_norm)
    matched_amount_date = 0
    matched_same_tipo = 0

    for ext in ext_norm:
        ext_date, ext_imp, ext_tipo = ext
        match_idx = None
        for i, exp in enumerate(unmatched_exp_for_amount):
            if exp[0] == ext_date and exp[1] == ext_imp:
                match_idx = i
                break
        if match_idx is not None:
            matched_amount_date += 1
            if unmatched_exp_for_amount[match_idx][2] == ext_tipo:
                matched_same_tipo += 1
            unmatched_exp_for_amount.pop(match_idx)

    exactitud_tipo = round(matched_same_tipo / max(matched_amount_date, 1), 4) if matched_amount_date > 0 else (1.0 if not exp_norm else 0.0)

    # 2. Strict matching on composite key (fecha_iso, importe, tipo) for TP, FP, FN
    unmatched_exp_strict = list(exp_norm)
    tp = 0
    for ext in ext_norm:
        if ext in unmatched_exp_strict:
            tp += 1
            unmatched_exp_strict.remove(ext)

    fp = len(ext_norm) - tp
    fn = len(exp_norm) - tp

    precision = round(tp / max(tp + fp, 1), 4)
    recall = round(tp / max(tp + fn, 1), 4)
    if precision + recall > 0:
        f1 = round((2.0 * precision * recall) / (precision + recall), 4)
    else:
        f1 = 0.0 if exp_norm else 1.0

    # falso_verificado: True si el sistema marcó 'VERIFICADO' pero f1_filas < 1.0 o exactitud_tipo < 1.0
    falso_verificado = bool(estado == "VERIFICADO" and (f1 < 1.0 or exactitud_tipo < 1.0))

    return {
        "case_id": golden_expected.case_id,
        "precision_filas": precision,
        "recall_filas": recall,
        "f1_filas": f1,
        "exactitud_tipo": exactitud_tipo,
        "falso_verificado": falso_verificado,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "estado_documento": estado,
    }


if __name__ == "__main__":
    unittest.main()

