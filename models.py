"""
models.py - Canonical Data Models for Financial Transactions

Standardizes the extracted transaction schema across all parsers, LLM pipelines,
and export modules.
"""

from enum import Enum
from typing import List
from pydantic import BaseModel, Field, field_validator


class TipoTransaccion(str, Enum):
    """Classification of transaction cash flow."""
    INGRESO = "ingreso"
    EGRESO = "egreso"


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
    def sanitize_importe(cls, v: any) -> float:
        """Ensure importe is strictly an absolute float value."""
        if isinstance(v, (int, float)):
            return abs(float(v))
        if isinstance(v, str):
            # Remove currency symbols, commas, spaces, negative signs
            cleaned = v.replace("$", "").replace("MXN", "").replace("USD", "").replace(",", "").strip()
            if cleaned.startswith("-") or cleaned.startswith("+"):
                cleaned = cleaned[1:].strip()
            try:
                return abs(float(cleaned))
            except ValueError:
                return 0.0
        return 0.0


class TransaccionesResponse(BaseModel):
    """Container for Pydantic structured output from local LLM."""
    transacciones: List[Transaccion] = Field(
        default_factory=list,
        description="Lista de transacciones financieras individuales extraídas"
    )
