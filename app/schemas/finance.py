import uuid
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────
# Telegram webhook
# ─────────────────────────────────────────

class TelegramFile(BaseModel):
    file_id: str
    file_unique_id: str
    duration: Optional[int] = None

class TelegramChat(BaseModel):
    id: int

class TelegramUser(BaseModel):
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None

class TelegramMessage(BaseModel):
    message_id: int
    from_user: Optional[TelegramUser] = Field(None, alias="from")
    chat: TelegramChat
    text: Optional[str] = None
    voice: Optional[TelegramFile] = None
    audio: Optional[TelegramFile] = None
    model_config = {"populate_by_name": True}

class TelegramCallbackMessage(BaseModel):
    message_id: int
    chat: TelegramChat

class TelegramCallbackQuery(BaseModel):
    id: str
    from_user: TelegramUser = Field(alias="from")
    message: Optional[TelegramCallbackMessage] = None
    data: Optional[str] = None
    model_config = {"populate_by_name": True}

class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessage] = None
    callback_query: Optional[TelegramCallbackQuery] = None


# ─────────────────────────────────────────
# LLM output — lo que extrae Ollama
# tipo y origen se derivan en código, no se piden al LLM
# ─────────────────────────────────────────

class LLMTransactionOutput(BaseModel):
    amount: float = Field(gt=0)
    currency: str = "EUR"
    categoria: str
    subcategoria1: str
    subcategoria2: Optional[str] = None
    subcategoria3: Optional[str] = None  # solo para Transporte
    nota: Optional[str] = None
    transaction_date: date
    confidence: float = Field(ge=0.0, le=1.0)
    medio_pago: Optional[str] = None  # 'tarjeta_credito' | 'efectivo' | 'cuenta' | None
    cuenta_hint: Optional[str] = None  # 'hector' | 'luisiana' | 'casa' | None

    @field_validator("transaction_date", mode="before")
    @classmethod
    def default_date_if_empty(cls, v):
        if not v:
            return date.today()
        return v


class LLMTransactionListOutput(BaseModel):
    transactions: list[LLMTransactionOutput]


# ─────────────────────────────────────────
# API responses
# ─────────────────────────────────────────

class TransactionOut(BaseModel):
    id: uuid.UUID
    transaction_date: date
    tipo: Optional[str]
    amount: float
    currency: str
    categoria: Optional[str]
    subcategoria1: Optional[str]
    subcategoria2: Optional[str]
    subcategoria3: Optional[str]
    nota: Optional[str]
    origen: Optional[str]
    llm_confidence: Optional[float]
    created_at: datetime
    model_config = {"from_attributes": True}


class TransactionSummaryItem(BaseModel):
    subcategoria1: Optional[str]
    total: float
