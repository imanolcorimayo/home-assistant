import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class UserRole(enum.Enum):
    admin = "admin"
    member = "member"


class FamilyMember(Base):
    __tablename__ = "family_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", create_type=False),
        nullable=False,
        default=UserRole.member,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="family_member")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    family_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_members.id"), nullable=False
    )
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    tipo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)           # 'ingreso' | 'gasto'
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    categoria: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria3: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Transporte: Combustible…
    nota: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origen: Mapped[Optional[str]] = mapped_column(Text, nullable=True)         # telegram | whatsapp | …
    llm_confidence: Mapped[Optional[float]] = mapped_column(Numeric(4, 3), nullable=True)
    llm_raw_output: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    family_member: Mapped[FamilyMember] = relationship(back_populates="transactions")


class MonthlyBudget(Base):
    __tablename__ = "monthly_budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    subcategoria1: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    limit_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
