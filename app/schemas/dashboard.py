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
