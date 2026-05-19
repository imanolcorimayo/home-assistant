"""SQLAlchemy 2.0 models mirroring migrations/schema.sql.

Conventions follow the SQL schema:
  · singular table names               (family_member)
  · PK named {table}_id                (family_member_id)
  · timestamps end in _ts              (created_ts, updated_ts)
  · dates end in _date                 (transaction_date)

Enums use create_type=False because the Postgres enums are already
created by schema.sql — SQLAlchemy must not try to CREATE TYPE again.
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Numeric,
    String,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AccountKind(enum.Enum):
    checking = "checking"
    savings = "savings"
    cash = "cash"
    credit_card = "credit_card"


class TransactionKind(enum.Enum):
    income = "income"
    expense = "expense"


class TransactionSource(enum.Enum):
    manual = "manual"
    telegram = "telegram"
    recurring = "recurring"
    whatsapp = "whatsapp"


class FamilyMember(Base):
    __tablename__ = "family_member"

    family_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_user_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_ts: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    accounts: Mapped[list["Account"]] = relationship(back_populates="family_member")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="family_member")


class Account(Base):
    __tablename__ = "account"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    family_member_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_member.family_member_id"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[AccountKind] = mapped_column(
        SAEnum(AccountKind, name="account_kind", create_type=False, native_enum=True),
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    initial_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, server_default="0")
    balance_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_ts: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    family_member: Mapped[Optional[FamilyMember]] = relationship(back_populates="accounts")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="account")


class RecurringCharge(Base):
    __tablename__ = "recurring_charge"
    __table_args__ = (
        CheckConstraint("amount > 0", name="recurring_charge_amount_check"),
        CheckConstraint("day_of_month BETWEEN 1 AND 31", name="recurring_charge_dom_check"),
    )

    recurring_charge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    day_of_month: Mapped[int] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    subcategory_1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategory_2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_ts: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class MonthlyBudget(Base):
    __tablename__ = "monthly_budget"
    __table_args__ = (
        CheckConstraint("limit_amount > 0", name="monthly_budget_limit_check"),
    )

    monthly_budget_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    subcategory_1: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_ts: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class Transaction(Base):
    __tablename__ = "transaction"
    __table_args__ = (
        CheckConstraint("amount > 0", name="transaction_amount_check"),
        CheckConstraint(
            "llm_confidence IS NULL OR (llm_confidence BETWEEN 0 AND 1)",
            name="transaction_llm_confidence_check",
        ),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("account.account_id"), nullable=False
    )
    family_member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("family_member.family_member_id"), nullable=False
    )
    recurring_charge_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("recurring_charge.recurring_charge_id"), nullable=True
    )
    kind: Mapped[TransactionKind] = mapped_column(
        SAEnum(TransactionKind, name="transaction_kind", create_type=False, native_enum=True),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="EUR")
    category: Mapped[str] = mapped_column(Text, nullable=False)
    subcategory_1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    subcategory_2: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transaction_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    value_date: Mapped[date] = mapped_column(Date, nullable=False, server_default=func.current_date())
    source: Mapped[TransactionSource] = mapped_column(
        SAEnum(TransactionSource, name="transaction_source", create_type=False, native_enum=True),
        nullable=False,
        server_default="manual",
    )
    llm_confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 3), nullable=True)
    llm_raw_output: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_ts: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    deleted_ts: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    account: Mapped[Account] = relationship(back_populates="transactions")
    family_member: Mapped[FamilyMember] = relationship(back_populates="transactions")
