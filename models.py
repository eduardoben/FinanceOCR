"""
models.py - Canonical Data Models for Financial Transactions

Standardizes the extracted transaction schema across all parsers, LLM pipelines,
and export modules.
"""

import re
from enum import Enum
from typing import List, Optional, Any, Dict, Literal
from pydantic import BaseModel, Field, field_validator


class TipoTransaccion(str, Enum):
    """Classification of transaction cash flow."""
    INGRESO = "ingreso"
    EGRESO = "egreso"

    @classmethod
    def _missing_(cls, value: Any):
        if isinstance(value, str):
            val_clean = value.strip().lower()
            for member in cls:
                if member.value == val_clean or member.name.lower() == val_clean:
                    return member
        return None


def normalizar_tipo_transaccion(tipo: str) -> str:
    """
    Normaliza el campo tipo convirtiendo variantes comunes de flujos bancarios:
    'cargo', 'egreso', 'retiro', 'compra' -> 'EGRESO'
    'abono', 'ingreso', 'deposito', 'pago' -> 'INGRESO'
    """
    if not tipo or not isinstance(tipo, str):
        return "EGRESO"
    clean = tipo.strip().lower()
    if clean in ("abono", "ingreso", "deposito", "pago", "spei recibido", "reembolso", "devolucion", "bonificacion"):
        return "INGRESO"
    if clean in ("cargo", "egreso", "retiro", "compra", "cgo", "disposicion", "comision", "iva", "spei enviado"):
        return "EGRESO"
    if "ingres" in clean or "abon" in clean or "deposit" in clean or "pag" in clean:
        return "INGRESO"
    return "EGRESO"


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
    # 1. Parentheses: e.g. (1,234.56), ($1,234.56), or (1,234.56 MXN)
    if re.search(r"\([^)]*\d+[^)]*\)", raw):
        is_negative = True
    # 2. Trailing minus: e.g. 1,234.56- or $1,234.56-
    elif re.search(r"[-−–]\s*$", raw) or re.search(r"\d+.*[-−–]\s*$", raw):
        is_negative = True
    # 3. Leading minus: e.g. -1,234.56 or -$1,234.56
    elif re.search(r"^\s*[-−–]", raw) or re.search(r"[-−–]\s*\$?\s*\d", raw):
        is_negative = True
    # 4. Accounting debit suffixes: DR or DB (overdraft/saldo deudor)
    elif re.search(r"\b(DR|DB)\b", raw, re.I) and not re.search(r"\bCR\b", raw, re.I):
        is_negative = True

    # Remove currency abbreviations and accounting keywords
    cleaned = re.sub(r"(?i)\b(MXN|USD|CR|DR|DB|CARGO|ABONO)\b", "", raw)
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
    confianza: float = Field(
        default=1.0,
        description="Puntaje de confianza en la extracción (0.0 a 1.0)"
    )
    origen: str = Field(
        default="llm",
        description="Canal o técnica de extracción ('llm', 'regex', 'xml', 'manual')"
    )
    linea_origen: str = Field(
        default="",
        description="Línea de texto crudo del documento original correspondiente a la transacción"
    )
    saldo_renglon: Optional[float] = Field(
        default=None,
        description="Saldo posterior reportado explícitamente en el renglón del estado de cuenta"
    )
    estado_revision: str = Field(
        default="pendiente",
        description="Estado de auditoría o revisión ('VERIFICADO', 'REVISAR', 'pendiente', 'rechazado')"
    )
    requiere_verificacion_llm: bool = Field(
        default=False,
        description="Indica si la fila requiere confirmación con LLM debido a celdas desplazadas o montos ambiguos"
    )
    pagina: int = Field(
        default=1,
        description="Número de página del documento donde se ubica la transacción"
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


class TransaccionPlana(BaseModel):
    """
    Flat Pydantic schema for Ollama structured output using simple primitive types.
    Matches the Ollama 'format' specification.
    """
    fecha: str = Field(..., description="Fecha de la transacción (YYYY-MM-DD o variante textual)")
    descripcion: str = Field(..., description="Descripción detallada o concepto del movimiento")
    importe: float = Field(..., description="Monto monetario de la transacción")
    tipo: str = Field(..., description="Tipo de movimiento: cargo, abono, egreso, ingreso, compra, pago, etc.")
    referencia: str = Field(default="", description="Referencia bancaria, folio, autorización o clave de rastreo")
    saldo_renglon: Optional[float] = Field(default=None, description="Saldo posterior al movimiento si aparece en el renglón")
    linea_origen: str = Field(default="", description="Línea o texto crudo original del documento")
    pagina: int = Field(default=1, description="Número de página del documento donde se ubica la transacción")


class LLMTransaccionesList(BaseModel):
    """
    Flat container schema for Ollama structured output parameter 'format'.
    """
    transacciones: List[TransaccionPlana] = Field(
        default_factory=list,
        description="Lista de transacciones planas extraídas por el LLM"
    )


class GoldenCaseExpected(BaseModel):
    """
    Schema for verified reference test cases in the Golden Dataset.
    Enforces absolute ground-truth accounting metadata and manually validated transactions.
    """
    case_id: str = Field(..., description="Identificador único del caso (ej. 'bbva_debito_digital')")
    banco: str = Field(..., description="Institución bancaria o emisora (ej. 'BBVA', 'Citibanamex')")
    tipo_documento: Literal["pdf_digital", "pdf_escaneado", "xml"] = Field(..., description="Tipo de entrada original")
    modo_esperado: Literal["debito", "credito"] = Field(..., description="Modo contable canónico esperado")
    saldo_inicial: float = Field(..., description="Saldo inicial reportado en el estado de cuenta")
    saldo_final: float = Field(..., description="Saldo final reportado al corte")
    total_cargos: float = Field(..., description="Monto acumulado esperado de cargos")
    total_abonos: float = Field(..., description="Monto acumulado esperado de abonos")
    movimientos_esperados: List[Transaccion] = Field(..., description="Lista de transacciones validadas manualmente")


class TransaccionesResponse(BaseModel):
    """Container for Pydantic structured output from local LLM."""
    transacciones: List[Transaccion] = Field(
        default_factory=list,
        description="Lista de transacciones financieras individuales extraídas"
    )


def auditar_fila_rechazada(
    linea_origen: str,
    datos_crudos: Any,
    motivo: str,
    detalle_error: str
) -> Dict[str, Any]:
    """
    Construye un registro estructurado de auditoría para filas descartadas.
    motivo: 'validacion_esquema' | 'importe_cero' | 'fecha_invalida'
    """
    return {
        "linea_origen": str(linea_origen),
        "datos_crudos": str(datos_crudos),
        "motivo": motivo,
        "detalle_error": str(detalle_error)
    }


def calcular_metricas_captura(
    filas_candidatas: int,
    filas_extraidas: int,
    filas_rechazadas: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Calcula las métricas de captura por documento:
    cobertura = filas_extraidas / max(filas_candidatas, 1)
    tasa_rechazo = len(filas_rechazadas) / max(filas_candidatas, 1)
    """
    cand = max(int(filas_candidatas), 1)
    cobertura = round(filas_extraidas / cand, 4)
    tasa_rechazo = round(len(filas_rechazadas) / cand, 4)
    return {
        "filas_candidatas": filas_candidatas,
        "filas_extraidas": filas_extraidas,
        "filas_rechazadas": filas_rechazadas,
        "cobertura": cobertura,
        "tasa_rechazo": tasa_rechazo
    }


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
    modo_calculo: str = "debito"  # "debito", "credito", "ambiguo", or "no_disponible"
    modo_contable: str = "debito"  # "debito", "credito", "ambiguo", or "no_disponible"
    cumplimiento_cadena_saldo: Optional[float] = None  # Porcentaje (0.0 a 100.0) de coincidencia de saldo renglón
    filas_con_saldo: int = 0
    filas_cumplen_cadena: int = 0
    estado_conciliacion: Optional[str] = None  # 'VERIFICADO', 'REVISAR', 'RECHAZADO'

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

        if "modo_contable" in data and "modo_calculo" not in data:
            data["modo_calculo"] = data["modo_contable"]
        elif "modo_calculo" in data and "modo_contable" not in data:
            data["modo_contable"] = data["modo_calculo"]

        # Ambiguous mode cannot be reconciled
        if data.get("modo_contable") == "ambiguo" or data.get("modo_calculo") == "ambiguo":
            data["cuadra"] = False
            data["conciliado"] = False

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
        if not self.modo_contable and self.modo_calculo:
            self.modo_contable = self.modo_calculo
        if not self.modo_calculo and self.modo_contable:
            self.modo_calculo = self.modo_contable

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
        modo = kwargs.get("modo_contable") or kwargs.get("modo_calculo") or "debito"

        if modo == "debito":
            calc = round(s_ini + abonos - cargos, 2)
            diff = round(abs(s_esp - calc), 2)
            cuadra = diff <= 0.01
        elif modo == "credito":
            calc = round(s_ini - abonos + cargos, 2)
            diff = round(abs(s_esp - calc), 2)
            cuadra = diff <= 0.01
        else:
            calc = round(s_ini + abonos - cargos, 2)
            diff = round(abs(s_esp - calc), 2)
            cuadra = False
            modo = "ambiguo"

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
            modo_calculo=modo,
            modo_contable=modo,
            cumplimiento_cadena_saldo=kwargs.get("cumplimiento_cadena_saldo"),
            filas_con_saldo=kwargs.get("filas_con_saldo", 0),
            filas_cumplen_cadena=kwargs.get("filas_cumplen_cadena", 0),
        )

    def calcular_cuadre(self) -> "ReconciliacionResultado":
        """Re-evaluates calculation and sets cuadra/conciliado status."""
        if self.saldo_inicial is not None and self.saldo_final_esperado is not None:
            s_ini = round(float(self.saldo_inicial), 2)
            abonos = round(float(self.total_abonos), 2)
            cargos = round(float(self.total_cargos), 2)
            s_esp = round(float(self.saldo_final_esperado), 2)

            if self.modo_contable == "credito":
                self.saldo_final_calculado = round(s_ini - abonos + cargos, 2)
                self.diferencia = round(abs(s_esp - self.saldo_final_calculado), 2)
                self.cuadra = self.diferencia <= 0.01
            elif self.modo_contable == "debito":
                self.saldo_final_calculado = round(s_ini + abonos - cargos, 2)
                self.diferencia = round(abs(s_esp - self.saldo_final_calculado), 2)
                self.cuadra = self.diferencia <= 0.01
            else:
                self.saldo_final_calculado = round(s_ini + abonos - cargos, 2)
                self.diferencia = round(abs(s_esp - self.saldo_final_calculado), 2)
                self.cuadra = False
                self.modo_contable = "ambiguo"
                self.modo_calculo = "ambiguo"

            self.conciliado = self.cuadra
            self.saldo_final_declarado = s_esp
        return self


def compute_row_confidence(
    tx: Transaccion,
    cadena_ok: Optional[bool] = None,
    acuerdo_senales: bool = True,
    fecha_en_periodo: bool = True
) -> float:
    """
    Calcula la puntuación ponderada de confianza de una fila (0.0 a 1.0).
    - Base por origen:
      * 'determinista_xml': 1.0
      * 'determinista_tabla': 0.85
      * 'llm': 0.60
      * 'heuristico': 0.45
      * otro: 0.50
    - Ajuste por cadena de saldo (cadena_ok):
      * True: +0.15
      * False: -0.25
      * None: 0.0 (cuando la tabla/extracto no posee columna de saldo)
    - Ajuste por acuerdo de señales (acuerdo_senales):
      * False: -0.15 (contradicción entre columna, palabras clave o signo)
    - Ajuste por fecha fuera del periodo del estado de cuenta (fecha_en_periodo):
      * False: -0.20
    
    Acota el resultado en [0.0, 1.0] redondeado a 2 decimales.
    Si confianza < 0.75, asigna automáticamente tx.estado_revision = 'REVISAR';
    de lo contrario, tx.estado_revision = 'VERIFICADO'.
    """
    origen = str(getattr(tx, "origen", "llm")).lower()
    if "xml" in origen:
        base = 1.0
    elif "multipass" in origen or "verificado" in origen:
        base = 0.85
    elif "tabla" in origen:
        base = 0.85
    elif "heurist" in origen:
        base = 0.45
    elif "llm" in origen:
        base = 0.60
    else:
        base = 0.50

    score = base
    if cadena_ok is True:
        score += 0.15
    elif cadena_ok is False:
        score -= 0.25

    if not acuerdo_senales:
        score -= 0.15

    if not fecha_en_periodo:
        score -= 0.20

    conf = max(0.0, min(1.0, round(score, 2)))
    tx.confianza = conf
    tx.estado_revision = "REVISAR" if conf < 0.75 else "VERIFICADO"
    return conf


def clasificar_estado_documento(
    cuadra: Optional[bool],
    modo_contable: str,
    cobertura: float = 1.0,
    filas_rechazadas_count: int = 0,
    tiene_saldos: bool = True,
    filas_baja_confianza_count: int = 0,
) -> str:
    """
    Clasifica el resultado del documento en uno de tres estados:
    - 'VERIFICADO': cuadra == True, modo_contable != 'ambiguo', cobertura >= 0.99 y filas_baja_confianza_count == 0.
    - 'REVISAR': cuadra == True pero con cobertura < 0.99, filas_rechazadas > 0, sin saldos extraídos o filas con confianza < 0.75.
    - 'RECHAZADO': cuadra == False o modo_contable == 'ambiguo'.
    """
    if modo_contable == "ambiguo" or cuadra is False:
        return "RECHAZADO"
    if not tiene_saldos or cuadra is None:
        return "REVISAR"
    if cuadra is True:
        if cobertura < 0.99 or filas_rechazadas_count > 0 or filas_baja_confianza_count > 0:
            return "REVISAR"
        return "VERIFICADO"
    return "RECHAZADO"


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
