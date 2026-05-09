from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class PresupuestoIn(BaseModel):
    subcategoria1: str = Field(min_length=1, max_length=80)
    limit_amount: float = Field(gt=0)


class MovimientoUpdate(BaseModel):
    tipo: Optional[str] = Field(default=None, pattern=r"^(ingreso|gasto)$")
    categoria: Optional[str] = Field(default=None, max_length=80)
    subcategoria1: Optional[str] = Field(default=None, max_length=80)
    subcategoria2: Optional[str] = Field(default=None, max_length=80)
    subcategoria3: Optional[str] = Field(default=None, max_length=80)
    amount: Optional[float] = Field(default=None, gt=0)
    nota: Optional[str] = Field(default=None, max_length=500)
    transaction_date: Optional[date] = None


class CuentaUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=80)
    saldo_inicial: Optional[float] = Field(default=None)
    saldo_fecha: Optional[date] = None
    cierre_dia: Optional[int] = Field(default=None, ge=1, le=31)
    vencimiento_dia: Optional[int] = Field(default=None, ge=1, le=31)
    activa: Optional[bool] = None


class PrestamoIn(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    cuenta_pago_id: str
    monto_cuota: float = Field(gt=0)
    dia_vencimiento: int = Field(ge=1, le=31)
    fecha_inicio: date
    fecha_fin: date
    monto_ultima_cuota: Optional[float] = Field(default=None, gt=0)
    notas: Optional[str] = Field(default=None, max_length=500)


class PrestamoUpdate(BaseModel):
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=80)
    cuenta_pago_id: Optional[str] = None
    monto_cuota: Optional[float] = Field(default=None, gt=0)
    dia_vencimiento: Optional[int] = Field(default=None, ge=1, le=31)
    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None
    monto_ultima_cuota: Optional[float] = Field(default=None, gt=0)
    notas: Optional[str] = Field(default=None, max_length=500)
    activo: Optional[bool] = None
