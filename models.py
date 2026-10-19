"""
models.py - Canonical Data Models for Financial Transactions

Standardizes the extracted transaction schema across all parsers, LLM pipelines,
and export modules.
"""

import re
from enum import Enum
from typing import List, Optional, Any
from pydantic import BaseModel, Field, field_validator


class TipoTransaccion(str, Enum):
    """Classification of transaction cash flow."""
    INGRESO = "ingreso"
    EGRESO = "egreso"


def sanitize_importe(v: any, keep_sign: bool = False) -> float:
    """
    Sanitizes monetary amounts from financial/accounting representations.
    
    Handles:
    - Accounting parentheses: (1,234.56) or ($1,234.56)
    - Accounting trailing minus: 1,234.56- or $1,234.56-
    - Standard negative signs: -1,234.56
    - Currency symbols: $, MXN, USD, spaces, commas
    - Accounting suffixes: CR, DR, CARGO, ABONO
    
    Args:
        v: Raw amount (int, float, str, or None).
        keep_sign: If True, preserves negative sign for accounting formats.
                   If False (default), returns the absolute positive float.
    
    Returns:
        float: Normalized float value.
    """
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        val = float(v)
        return val if keep_sign else abs(val)

    if not isinstance(v, str):
        return 0.0

    raw = v.strip()
    if not raw:
        return 0.0

    # Detect accounting negative format
    is_negative = False
    # 1. Parentheses: e.g. (1,234.56) or ($1,234.56)
    if re.search(r"^\s*\([^)]*\)\s*$", raw):
        is_negative = True
    # 2. Trailing minus: e.g. 1,234.56- or $1,234.56-
    elif re.search(r"-\s*$", raw):
        is_negative = True
    # 3. Leading minus: e.g. -1,234.56 or -$1,234.56
    elif re.search(r"^\s*[-−–]", raw):
        is_negative = True

    # Remove currency abbreviations and accounting keywords
    cleaned = re.sub(r"(?i)\b(MXN|USD|CR|DR|CARGO|ABONO)\b", "", raw)
    # Remove currency symbols, commas, spaces, parentheses, minus signs
    cleaned = (
        cleaned.replace("$", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .replace("−", "")
        .replace("–", "")
        .strip()
    )

    # Extract first valid decimal numeric pattern
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if match:
        try:
            num = float(match.group(0))
            if is_negative and keep_sign:
                return -num
            return abs(num)
        except ValueError:
            return 0.0

    return 0.0


class Transaccion(BaseModel):
    """
    Standardized financial transaction model.
    Matches the required Excel export schema:
    1. fecha: YYYY-MM-DD
    2. concepto: Short, clean merchant/entity name
    3. importe: Absolute float number (e.g., 1250.50)
    4. descripción: Extended details, bank references, SPEI/folio, RFC
    5. tipo: 'ingreso' or 'egreso'
    """
    fecha: str = Field(
        ...,
        description="Fecha de la transacción normalizada en formato YYYY-MM-DD"
    )
    concepto: str = Field(
        ...,
        description="Nombre corto y limpio del comercio, entidad o concepto principal"
    )
    importe: float = Field(
        ...,
        description="Monto absoluto en número decimal positivo (sin signos ni comas de miles)"
    )
    descripcion: str = Field(
        ...,
        description="Detalles extendidos, referencias bancarias, clave de rastreo SPEI, RFC o folio"
    )
    tipo: TipoTransaccion = Field(
        ...,
        description="Clasificación del flujo: 'ingreso' (abono/depósito) o 'egreso' (cargo/retiro/pago)"
    )

    @field_validator("importe", mode="before")
    @classmethod
    def sanitize_importe_validator(cls, v: any) -> float:
        """Ensure importe is strictly an absolute float value, handling accounting formats."""
        return sanitize_importe(v, keep_sign=False)

    @classmethod
    def sanitize_importe(cls, v: any, keep_sign: bool = False) -> float:
        """Backward-compatible classmethod access to amount sanitizer."""
        return sanitize_importe(v, keep_sign=keep_sign)


class TransaccionesResponse(BaseModel):
    """Container for Pydantic structured output from local LLM."""
    transacciones: List[Transaccion] = Field(
        default_factory=list,
        description="Lista de transacciones financieras individuales extraídas"
    )


class ReconciliacionResultado(BaseModel):
    """
    Container for arithmetic balance reconciliation metrics and financial certainty verification.
    Formula: Saldo Final Calculado = Saldo Inicial + Total Abonos - Total Cargos
    Diferencia = | Saldo Final Esperado - Saldo Final Calculado |
    Cuadra = Diferencia <= 0.01
    """
    saldo_inicial: Optional[float] = 0.0
    total_abonos: float = 0.0
    total_cargos: float = 0.0
    saldo_final_esperado: Optional[float] = 0.0
    saldo_final_calculado: Optional[float] = 0.0
    diferencia: Optional[float] = 0.0
    cuadra: Optional[bool] = False

    # Backward compatibility attributes with legacy names
    saldo_final_declarado: Optional[float] = None
    total_ingresos: float = 0.0
    total_egresos: float = 0.0
    conciliado: Optional[bool] = None
    modo_calculo: str = "debito"  # "debito", "credito", or "no_disponible"

    def __init__(self, **data: Any):
        # Synchronize backward-compatible aliases before initialization
        if "total_ingresos" in data and "total_abonos" not in data:
            data["total_abonos"] = data["total_ingresos"]
        elif "total_abonos" in data and "total_ingresos" not in data:
            data["total_ingresos"] = data["total_abonos"]

        if "total_egresos" in data and "total_cargos" not in data:
            data["total_cargos"] = data["total_egresos"]
        elif "total_cargos" in data and "total_egresos" not in data:
            data["total_egresos"] = data["total_cargos"]

        if "saldo_final_declarado" in data and "saldo_final_esperado" not in data:
            data["saldo_final_esperado"] = data["saldo_final_declarado"]
        elif "saldo_final_esperado" in data and "saldo_final_declarado" not in data:
            data["saldo_final_declarado"] = data["saldo_final_esperado"]

        if "conciliado" in data and "cuadra" not in data:
            data["cuadra"] = data["conciliado"]
        elif "cuadra" in data and "conciliado" not in data:
            data["conciliado"] = data["cuadra"]

        super().__init__(**data)

        # Synchronize reciprocal attributes
        if self.total_abonos and not self.total_ingresos:
            self.total_ingresos = self.total_abonos
        if self.total_cargos and not self.total_egresos:
            self.total_egresos = self.total_cargos
        if self.saldo_final_esperado is not None and self.saldo_final_declarado is None:
            self.saldo_final_declarado = self.saldo_final_esperado
        if self.cuadra is not None and self.conciliado is None:
            self.conciliado = self.cuadra

    @classmethod
    def validar(
        cls,
        saldo_inicial: float,
        total_abonos: float,
        total_cargos: float,
        saldo_final_esperado: float,
        **kwargs
    ) -> "ReconciliacionResultado":
        """
        Validates mathematical balance reconciliation:
        saldo_final_calculado = round(saldo_inicial + total_abonos - total_cargos, 2)
        diferencia = round(abs(saldo_final_esperado - saldo_final_calculado), 2)
        cuadra = diferencia <= 0.01
        """
        s_ini = round(float(saldo_inicial), 2)
        abonos = round(float(total_abonos), 2)
        cargos = round(float(total_cargos), 2)
        s_esp = round(float(saldo_final_esperado), 2)

        calc = round(s_ini + abonos - cargos, 2)
        diff = round(abs(s_esp - calc), 2)
        cuadra = diff <= 0.01

        return cls(
            saldo_inicial=s_ini,
            total_abonos=abonos,
            total_cargos=cargos,
            saldo_final_esperado=s_esp,
            saldo_final_calculado=calc,
            diferencia=diff,
            cuadra=cuadra,
            saldo_final_declarado=s_esp,
            total_ingresos=abonos,
            total_egresos=cargos,
            conciliado=cuadra,
            modo_calculo=kwargs.get("modo_calculo", "debito"),
        )

    def calcular_cuadre(self) -> "ReconciliacionResultado":
        """Re-evaluates calculation and sets cuadra/conciliado status."""
        if self.saldo_inicial is not None and self.saldo_final_esperado is not None:
            s_ini = round(float(self.saldo_inicial), 2)
            abonos = round(float(self.total_abonos), 2)
            cargos = round(float(self.total_cargos), 2)
            s_esp = round(float(self.saldo_final_esperado), 2)

            self.saldo_final_calculado = round(s_ini + abonos - cargos, 2)
            self.diferencia = round(abs(s_esp - self.saldo_final_calculado), 2)
            self.cuadra = self.diferencia <= 0.01
            self.conciliado = self.cuadra
            self.saldo_final_declarado = s_esp
        return self


def validar_reconciliacion(
    saldo_inicial: float,
    total_abonos: float,
    total_cargos: float,
    saldo_final_esperado: float,
    **kwargs
) -> ReconciliacionResultado:
    """Convenience helper to build a validated ReconciliacionResultado."""
    return ReconciliacionResultado.validar(
        saldo_inicial=saldo_inicial,
        total_abonos=total_abonos,
        total_cargos=total_cargos,
        saldo_final_esperado=saldo_final_esperado,
        **kwargs
    )
