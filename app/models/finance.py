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


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[str] = mapped_column(Text, nullable=False)  # 'corriente' | 'efectivo' | 'tarjeta_credito'
    family_member_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_members.id"), nullable=True
    )
    moneda: Mapped[str] = mapped_column(Text, nullable=False, default="EUR")
    saldo_inicial: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    saldo_fecha: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    activa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cierre_dia: Mapped[Optional[int]] = mapped_column(nullable=True)
    vencimiento_dia: Mapped[Optional[int]] = mapped_column(nullable=True)
    cuenta_pago_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class InstallmentPlan(Base):
    __tablename__ = "installment_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    fecha_compra: Mapped[date] = mapped_column(Date, nullable=False)
    descripcion: Mapped[str] = mapped_column(Text, nullable=False)
    monto_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    cuotas_total: Mapped[int] = mapped_column(nullable=False)
    monto_cuota: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    categoria: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class CardStatement(Base):
    __tablename__ = "card_statements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    fecha_cierre: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_vencimiento: Mapped[date] = mapped_column(Date, nullable=False)
    monto: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    cuenta_pago_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    pagado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pagado_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class RecurringCharge(Base):
    __tablename__ = "recurring_charges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    monto: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    dia_mes: Mapped[int] = mapped_column(nullable=False)
    categoria: Mapped[str] = mapped_column(Text, nullable=False, default="Gastos variables")
    subcategoria1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategoria2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fecha_inicio: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    fecha_fin: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Loan(Base):
    __tablename__ = "loans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    cuenta_pago_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    monto_cuota: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    dia_vencimiento: Mapped[int] = mapped_column(nullable=False)
    fecha_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_fin: Mapped[date] = mapped_column(Date, nullable=False)
    monto_ultima_cuota: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    notas: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    family_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_members.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("loans.id"), nullable=True
    )
    installment_plan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("installment_plans.id"), nullable=True
    )
    recurring_charge_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurring_charges.id"), nullable=True
    )
    card_statement_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("card_statements.id"), nullable=True
    )
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_valor: Mapped[date] = mapped_column(Date, nullable=False)
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
