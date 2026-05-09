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


class InstallmentPlanIn(BaseModel):
    account_id: str
    fecha_compra: date
    descripcion: str = Field(min_length=1, max_length=120)
    monto_total: float = Field(gt=0)
    cuotas_total: int = Field(ge=1, le=120)
    monto_cuota: Optional[float] = Field(default=None, gt=0)  # si no, monto_total/cuotas
    categoria: Optional[str] = Field(default=None, max_length=80)
    subcategoria1: Optional[str] = Field(default=None, max_length=80)
    subcategoria2: Optional[str] = Field(default=None, max_length=80)
    notas: Optional[str] = Field(default=None, max_length=300)


class SuscripcionIn(BaseModel):
    account_id: str
    nombre: str = Field(min_length=1, max_length=80)
    monto: float = Field(gt=0)
    dia_mes: int = Field(ge=1, le=31)
    categoria: Optional[str] = Field(default="Gastos variables", max_length=80)
    subcategoria1: Optional[str] = Field(default=None, max_length=80)
    subcategoria2: Optional[str] = Field(default=None, max_length=80)
    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None


class SuscripcionUpdate(BaseModel):
    account_id: Optional[str] = None
    nombre: Optional[str] = Field(default=None, min_length=1, max_length=80)
    monto: Optional[float] = Field(default=None, gt=0)
    dia_mes: Optional[int] = Field(default=None, ge=1, le=31)
    categoria: Optional[str] = Field(default=None, max_length=80)
    subcategoria1: Optional[str] = Field(default=None, max_length=80)
    subcategoria2: Optional[str] = Field(default=None, max_length=80)
    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None
    activo: Optional[bool] = None


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
