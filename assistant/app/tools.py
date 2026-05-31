"""Registrar tools — the functions the agent can call to keep the ledger.

Ported from household's MCP server (mcp_server.py + services/transactions.py +
services/recurring.py), with three deliberate changes for the new app:

  1. MULTI-TENANT: every function takes `family_id` and scopes every query to
     it (`WHERE family_id = $1`). By-id operations (edit/delete/pay) ALSO check
     family_id so one family can never touch another's rows. family_id comes
     from the session — never from the model.
  2. IDENTITY FROM SESSION: writes are attributed to `member_id` (the logged-in
     user), passed in by the agent layer. There is no "register on behalf of X"
     hint and no create_member tool — members come only from Google login.
  3. RAW asyncpg, not SQLAlchemy. `category_id`/`account_id` are NOT NULL now,
     so we lazily get-or-create a default "Sin categoría" category and an
     "Efectivo" account per family instead of allowing NULLs.

These are plain async functions; the agent layer wraps each in a closure that
binds family_id/member_id so the model never sees those as parameters.
"""

import logging
import uuid
from datetime import date as date_cls
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from app import db

log = logging.getLogger("tools")

DEFAULT_CATEGORY = "Sin categoría"
DEFAULT_ACCOUNT = "Efectivo"
_VALID_GRUPOS = {"variable", "fijo", "ingreso"}
_VALID_KINDS = {"checking", "savings", "cash", "credit_card"}


# ── parsing helpers ────────────────────────────────────────────────────────

def _parse_amount(raw) -> Decimal | None:
    if raw is None:
        return None
    try:
        amt = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return amt if amt > 0 else None


def _parse_date(raw) -> date_cls | None:
    if not raw:
        return None
    try:
        return date_cls.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _as_uuid(raw) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


# ── lazy defaults (category_id / account_id are NOT NULL) ──────────────────

async def _ensure_default_category(family_id) -> uuid.UUID:
    """Get-or-create the family's 'Sin categoría' fallback row."""
    cid = await db.fetchval(
        "SELECT category_id FROM category WHERE family_id = $1 AND lower(name) = lower($2)",
        family_id, DEFAULT_CATEGORY,
    )
    if cid is not None:
        return cid
    return await db.fetchval(
        "INSERT INTO category (family_id, name) VALUES ($1, $2) RETURNING category_id",
        family_id, DEFAULT_CATEGORY,
    )


async def _ensure_default_account(family_id) -> uuid.UUID:
    """Get-or-create a shared 'Efectivo' cash account. Honours the rule that a
    transaction always has an account — the agent never has to ask."""
    aid = await db.fetchval(
        "SELECT account_id FROM account WHERE family_id = $1 AND is_active ORDER BY created_ts LIMIT 1",
        family_id,
    )
    if aid is not None:
        return aid
    return await db.fetchval(
        "INSERT INTO account (family_id, name, kind) VALUES ($1, $2, 'cash') RETURNING account_id",
        family_id, DEFAULT_ACCOUNT,
    )


async def _resolve_category_id(family_id, name) -> uuid.UUID:
    """Map a (fuzzy) category name to a category_id in this family. Falls back
    to the default category when missing/unknown."""
    if name and name.strip():
        cid = await db.fetchval(
            "SELECT category_id FROM category WHERE family_id = $1 AND lower(name) = lower($2) AND is_active",
            family_id, name.strip(),
        )
        if cid is not None:
            return cid
    return await _ensure_default_category(family_id)


async def _resolve_account_id(family_id, hint) -> uuid.UUID:
    """Fuzzy-match an account hint ('Visa', 'efectivo') to an account in this
    family; fall back to the default account."""
    if hint and hint.strip():
        needle = hint.strip()
        aid = await db.fetchval(
            "SELECT account_id FROM account WHERE family_id = $1 AND is_active AND lower(name) = lower($2)",
            family_id, needle,
        )
        if aid is None:
            aid = await db.fetchval(
                "SELECT account_id FROM account WHERE family_id = $1 AND is_active AND name ILIKE $2 LIMIT 1",
                family_id, f"%{needle}%",
            )
        if aid is not None:
            return aid
    return await _ensure_default_account(family_id)


# ── reads ──────────────────────────────────────────────────────────────────

async def active_category_names(family_id) -> list[str]:
    """Category names offered to the model (excludes the 'Sin categoría'
    fallback — that's reserved for 'none of these apply')."""
    rows = await db.fetch(
        """
        SELECT name FROM category
        WHERE family_id = $1 AND is_active AND name <> $2
        ORDER BY name
        """,
        family_id, DEFAULT_CATEGORY,
    )
    return [r["name"] for r in rows]


async def recent_transactions(
    family_id,
    limit: int = 20,
    days: int = 90,
    term: str | None = None,
    kind: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    """Recent transactions for this family, newest first. Joins category +
    member names. Backs both the prompt's pre-fed list and look_up_transactions."""
    since = date_cls.today() - timedelta(days=max(days, 1))
    clauses = ["t.family_id = $1", "t.transaction_date >= $2"]
    params: list = [family_id, since]
    if not include_deleted:
        clauses.append("t.deleted_ts IS NULL")
    if kind in ("expense", "income"):
        params.append(kind)
        clauses.append(f"t.kind = ${len(params)}")
    if term and term.strip():
        params.append(f"%{term.strip()}%")
        i = len(params)
        clauses.append(f"(t.description ILIKE ${i} OR c.name ILIKE ${i})")
    params.append(min(max(limit, 1), 50))
    rows = await db.fetch(
        f"""
        SELECT t.transaction_id, t.kind, t.transaction_date, t.amount,
               t.description, t.deleted_ts, t.recurring_charge_id,
               c.name AS category, m.full_name AS member, a.currency
        FROM transaction t
        JOIN category c ON c.category_id = t.category_id
        JOIN member   m ON m.member_id   = t.member_id
        JOIN account  a ON a.account_id  = t.account_id
        WHERE {' AND '.join(clauses)}
        ORDER BY t.transaction_date DESC, t.created_ts DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    return [
        {
            "id": str(r["transaction_id"]),
            "kind": r["kind"],
            "date": str(r["transaction_date"]),
            "amount": float(r["amount"]),
            "currency": r["currency"],
            "category": r["category"],
            "description": r["description"],
            "member": r["member"],
            "deleted": r["deleted_ts"] is not None,
            "recurring_charge_id": (
                str(r["recurring_charge_id"]) if r["recurring_charge_id"] else None
            ),
        }
        for r in rows
    ]


async def look_up_transactions(
    family_id,
    term: str | None = None,
    days: int = 90,
    limit: int = 15,
    kind: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    return await recent_transactions(
        family_id, limit=limit, days=days, term=term, kind=kind,
        include_deleted=include_deleted,
    )


# ── transaction writes ──────────────────────────────────────────────────────

async def create_transaction(
    family_id,
    member_id,
    kind: str,
    amount,
    description: str | None = None,
    category: str | None = None,
    transaction_date: str | None = None,
    account_hint: str | None = None,
    source: str = "chat",
    recurring_charge_id=None,
) -> dict:
    """Insert one transaction, attributed to member_id (the logged-in user).
    account_hint fuzzy-matches an account; category resolves to a category_id."""
    if kind not in ("expense", "income"):
        return {"ok": False, "error": "kind debe ser expense o income"}
    amt = _parse_amount(amount)
    if amt is None:
        return {"ok": False, "error": "monto inválido (debe ser > 0)"}

    account_id = await _resolve_account_id(family_id, account_hint)
    category_id = await _resolve_category_id(family_id, category)
    tx_date = _parse_date(transaction_date) or date_cls.today()

    try:
        tx_id = await db.fetchval(
            """
            INSERT INTO transaction
                (family_id, account_id, member_id, recurring_charge_id, category_id,
                 kind, amount, description, transaction_date, value_date, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $10)
            RETURNING transaction_id
            """,
            family_id, account_id, member_id, recurring_charge_id, category_id,
            kind, amt, description, tx_date, source,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("create_transaction failed: %s", exc)
        return {"ok": False, "error": "no se pudo registrar"}
    # Report the category/account ACTUALLY used — when the requested category
    # didn't exist we fell back to 'Sin categoría', and the agent must say so
    # rather than claim the category it asked for.
    cat_name = await db.fetchval("SELECT name FROM category WHERE category_id = $1", category_id)
    acct_name = await db.fetchval("SELECT name FROM account WHERE account_id = $1", account_id)
    return {"ok": True, "transaction_id": str(tx_id),
            "category_used": cat_name, "account_used": acct_name}


async def update_transaction(
    family_id,
    transaction_id: str,
    amount=None,
    description: str | None = None,
    category: str | None = None,
    transaction_date: str | None = None,
) -> dict:
    """Patch a non-deleted transaction (only fields passed change). Scoped to
    family_id so a foreign id can't be edited."""
    tid = _as_uuid(transaction_id)
    if tid is None:
        return {"ok": False, "error": "id inválido"}

    sets: list[str] = []
    params: list = []
    if amount is not None:
        amt = _parse_amount(amount)
        if amt is None:
            return {"ok": False, "error": "monto inválido (debe ser > 0)"}
        params.append(amt); sets.append(f"amount = ${len(params)}")
    if description is not None:
        params.append(description); sets.append(f"description = ${len(params)}")
    if category is not None:
        params.append(await _resolve_category_id(family_id, category))
        sets.append(f"category_id = ${len(params)}")
    if transaction_date is not None:
        d = _parse_date(transaction_date)
        if d is None:
            return {"ok": False, "error": "fecha inválida (YYYY-MM-DD)"}
        params.append(d); sets.append(f"transaction_date = ${len(params)}")
        params.append(d); sets.append(f"value_date = ${len(params)}")
    if not sets:
        return {"ok": False, "error": "nada para cambiar"}

    params.extend([tid, family_id])
    status = await db.execute(
        f"""
        UPDATE transaction SET {', '.join(sets)}
        WHERE transaction_id = ${len(params) - 1} AND family_id = ${len(params)}
          AND deleted_ts IS NULL
        """,
        *params,
    )
    if status.endswith("0"):
        return {"ok": False, "error": "no se encontró (id inexistente o borrado)"}
    return {"ok": True, "transaction_id": str(tid)}


async def _tx_snapshot(family_id, tid) -> dict | None:
    r = await db.fetchrow(
        """
        SELECT t.transaction_id, t.kind, t.amount, t.description,
               t.transaction_date, c.name AS category, m.full_name AS member,
               a.currency
        FROM transaction t
        JOIN category c ON c.category_id = t.category_id
        JOIN member   m ON m.member_id   = t.member_id
        JOIN account  a ON a.account_id  = t.account_id
        WHERE t.transaction_id = $1 AND t.family_id = $2
        """,
        tid, family_id,
    )
    if r is None:
        return None
    return {
        "transaction_id": str(r["transaction_id"]),
        "kind": r["kind"],
        "amount": float(r["amount"]),
        "currency": r["currency"],
        "category": r["category"],
        "description": r["description"],
        "transaction_date": str(r["transaction_date"]),
        "member": r["member"],
    }


async def soft_delete_transaction(family_id, transaction_id: str) -> dict:
    """Set deleted_ts (recoverable). Returns a snapshot of what was removed."""
    tid = _as_uuid(transaction_id)
    if tid is None:
        return {"ok": False, "error": "id inválido"}
    snap = await _tx_snapshot(family_id, tid)
    status = await db.execute(
        """
        UPDATE transaction SET deleted_ts = NOW()
        WHERE transaction_id = $1 AND family_id = $2 AND deleted_ts IS NULL
        """,
        tid, family_id,
    )
    if status.endswith("0"):
        return {"ok": False, "error": "no se encontró o ya estaba borrado"}
    return {"ok": True, "transaction": snap}


async def restore_transaction(family_id, transaction_id: str) -> dict:
    """Clear deleted_ts so the row re-enters reports."""
    tid = _as_uuid(transaction_id)
    if tid is None:
        return {"ok": False, "error": "id inválido"}
    status = await db.execute(
        """
        UPDATE transaction SET deleted_ts = NULL
        WHERE transaction_id = $1 AND family_id = $2 AND deleted_ts IS NOT NULL
        """,
        tid, family_id,
    )
    if status.endswith("0"):
        return {"ok": False, "error": "no se encontró o no estaba borrado"}
    return {"ok": True, "transaction": await _tx_snapshot(family_id, tid)}


# ── household structure ──────────────────────────────────────────────────────

async def create_category(family_id, name: str, grupo: str | None = None) -> dict:
    """Create a category (idempotent on name, case-insensitive)."""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}
    g = (grupo or "").strip().lower() or None
    if g is not None and g not in _VALID_GRUPOS:
        return {"ok": False, "error": "grupo debe ser variable, fijo o ingreso"}
    existing = await db.fetchrow(
        "SELECT name FROM category WHERE family_id = $1 AND lower(name) = lower($2)",
        family_id, clean,
    )
    if existing is not None:
        return {"ok": True, "name": existing["name"], "created": False}
    try:
        await db.execute(
            "INSERT INTO category (family_id, name, grupo) VALUES ($1, $2, $3)",
            family_id, clean, g,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("create_category failed: %s", exc)
        return {"ok": False, "error": "no se pudo crear la categoría"}
    return {"ok": True, "name": clean, "created": True}


async def create_categories(family_id, names: list[str], grupo: str | None = None) -> dict:
    """Create several categories at once (idempotent per name). Returns the lists
    of created vs already-existing names. grupo (optional) applies to all."""
    if not names:
        return {"ok": False, "error": "no se pasaron nombres"}
    g = (grupo or "").strip().lower() or None
    if g is not None and g not in _VALID_GRUPOS:
        return {"ok": False, "error": "grupo debe ser variable, fijo o ingreso"}
    created, existing = [], []
    for raw in names:
        clean = (raw or "").strip()
        if not clean:
            continue
        row = await db.fetchrow(
            "SELECT name FROM category WHERE family_id = $1 AND lower(name) = lower($2)",
            family_id, clean,
        )
        if row is not None:
            existing.append(row["name"])
            continue
        try:
            await db.execute(
                "INSERT INTO category (family_id, name, grupo) VALUES ($1, $2, $3)",
                family_id, clean, g,
            )
            created.append(clean)
        except Exception as exc:  # noqa: BLE001
            log.exception("create_categories: failed on %r: %s", clean, exc)
    return {"ok": True, "created": created, "existing": existing}


async def create_budget(family_id, category: str, limit_amount) -> dict:
    """Set/update the monthly budget for a category (one per category)."""
    clean = (category or "").strip()
    if not clean:
        return {"ok": False, "error": "la categoría no puede estar vacía"}
    amt = _parse_amount(limit_amount)
    if amt is None:
        return {"ok": False, "error": "límite inválido (debe ser > 0)"}
    category_id = await _resolve_category_id(family_id, clean)
    # Upsert on the UNIQUE(family_id, category_id) constraint.
    status = await db.execute(
        """
        INSERT INTO monthly_budget (family_id, category_id, limit_amount)
        VALUES ($1, $2, $3)
        ON CONFLICT (family_id, category_id)
        DO UPDATE SET limit_amount = EXCLUDED.limit_amount
        """,
        family_id, category_id, amt,
    )
    created = status.startswith("INSERT") and not status.endswith("0")
    return {"ok": True, "category": clean, "limit": float(amt), "created": created}


async def create_account(
    family_id,
    name: str,
    kind: str,
    currency: str = "EUR",
    initial_balance=0,
) -> dict:
    """Create a shared family account. kind: checking/savings/cash/credit_card."""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}
    k = (kind or "").strip().lower()
    if k not in _VALID_KINDS:
        return {"ok": False, "error": "kind debe ser checking, savings, cash o credit_card"}
    bal = _parse_amount(initial_balance) if str(initial_balance).strip() not in ("", "0") else Decimal("0")
    if bal is None:
        return {"ok": False, "error": "balance inicial inválido"}
    existing = await db.fetchrow(
        "SELECT account_id, name FROM account WHERE family_id = $1 AND is_active AND lower(name) = lower($2)",
        family_id, clean,
    )
    if existing is not None:
        return {"ok": True, "account_id": str(existing["account_id"]),
                "name": existing["name"], "created": False}
    try:
        aid = await db.fetchval(
            """
            INSERT INTO account (family_id, name, kind, currency, initial_balance)
            VALUES ($1, $2, $3, $4, $5) RETURNING account_id
            """,
            family_id, clean, k, (currency or "EUR").strip().upper()[:3] or "EUR", bal,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("create_account failed: %s", exc)
        return {"ok": False, "error": "no se pudo crear la cuenta"}
    return {"ok": True, "account_id": str(aid), "name": clean, "created": True}


# ── recurring charges ────────────────────────────────────────────────────────

def _coerce_dom(raw) -> int | None:
    try:
        dom = int(raw)
    except (TypeError, ValueError):
        return None
    return dom if 1 <= dom <= 31 else None


async def list_recurring(family_id, include_inactive: bool = False) -> list[dict]:
    """List recurring charges, each annotated with whether it's been paid this
    calendar month (derived from a linked transaction, not a stored flag)."""
    today = date_cls.today()
    month_start = today.replace(day=1)
    clauses = ["r.family_id = $1"]
    if not include_inactive:
        clauses.append("r.is_active")
    rows = await db.fetch(
        f"""
        SELECT r.recurring_charge_id, r.name, r.amount, r.day_of_month,
               r.is_active, c.name AS category,
               (SELECT max(t.transaction_date) FROM transaction t
                  WHERE t.recurring_charge_id = r.recurring_charge_id
                    AND t.deleted_ts IS NULL
                    AND t.transaction_date >= $2) AS last_paid
        FROM recurring_charge r
        JOIN category c ON c.category_id = r.category_id
        WHERE {' AND '.join(clauses)}
        ORDER BY r.day_of_month
        """,
        family_id, month_start,
    )
    return [
        {
            "id": str(r["recurring_charge_id"]),
            "name": r["name"],
            "amount": float(r["amount"]),
            "day_of_month": r["day_of_month"],
            "category": r["category"],
            "is_active": r["is_active"],
            "paid_this_month": r["last_paid"] is not None,
            "last_paid_date": str(r["last_paid"]) if r["last_paid"] else None,
        }
        for r in rows
    ]


async def create_recurring_charge(
    family_id,
    name: str,
    amount,
    day_of_month,
    category: str | None = None,
    account_hint: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Define a recurring charge (the definition only — not a payment)."""
    clean = (name or "").strip()
    if not clean:
        return {"ok": False, "error": "el nombre no puede estar vacío"}
    amt = _parse_amount(amount)
    if amt is None:
        return {"ok": False, "error": "monto inválido (debe ser > 0)"}
    dom = _coerce_dom(day_of_month)
    if dom is None:
        return {"ok": False, "error": "day_of_month debe ser un número entre 1 y 31"}
    account_id = await _resolve_account_id(family_id, account_hint)
    category_id = await _resolve_category_id(family_id, category)
    try:
        rid = await db.fetchval(
            """
            INSERT INTO recurring_charge
                (family_id, account_id, name, amount, day_of_month, category_id,
                 start_date, end_date)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING recurring_charge_id
            """,
            family_id, account_id, clean, amt, dom, category_id,
            _parse_date(start_date) or date_cls.today(), _parse_date(end_date),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("create_recurring_charge failed: %s", exc)
        return {"ok": False, "error": "no se pudo crear el recurrente"}
    return {"ok": True, "recurring_charge_id": str(rid)}


async def update_recurring_charge(
    family_id,
    recurring_charge_id: str,
    name: str | None = None,
    amount=None,
    day_of_month=None,
    category: str | None = None,
    end_date: str | None = None,
    is_active: bool | None = None,
) -> dict:
    """Edit a recurring charge's definition (only fields passed change)."""
    rid = _as_uuid(recurring_charge_id)
    if rid is None:
        return {"ok": False, "error": "id inválido"}
    sets: list[str] = []
    params: list = []
    if name is not None:
        params.append(name); sets.append(f"name = ${len(params)}")
    if amount is not None:
        amt = _parse_amount(amount)
        if amt is None:
            return {"ok": False, "error": "monto inválido (debe ser > 0)"}
        params.append(amt); sets.append(f"amount = ${len(params)}")
    if day_of_month is not None:
        dom = _coerce_dom(day_of_month)
        if dom is None:
            return {"ok": False, "error": "day_of_month debe ser un número entre 1 y 31"}
        params.append(dom); sets.append(f"day_of_month = ${len(params)}")
    if category is not None:
        params.append(await _resolve_category_id(family_id, category))
        sets.append(f"category_id = ${len(params)}")
    if end_date is not None:
        params.append(_parse_date(end_date)); sets.append(f"end_date = ${len(params)}")
    if is_active is not None:
        params.append(bool(is_active)); sets.append(f"is_active = ${len(params)}")
    if not sets:
        return {"ok": False, "error": "nada para cambiar"}
    params.extend([rid, family_id])
    status = await db.execute(
        f"""
        UPDATE recurring_charge SET {', '.join(sets)}
        WHERE recurring_charge_id = ${len(params) - 1} AND family_id = ${len(params)}
        """,
        *params,
    )
    if status.endswith("0"):
        return {"ok": False, "error": "no existe ese recurrente"}
    return {"ok": True, "recurring_charge_id": str(rid)}


async def pay_recurring(
    family_id,
    member_id,
    recurring_charge_id: str,
    transaction_date: str | None = None,
    amount=None,
) -> dict:
    """Register a recurring charge's payment by inserting a linked transaction —
    that linked row is what marks it paid for the month."""
    rid = _as_uuid(recurring_charge_id)
    if rid is None:
        return {"ok": False, "error": "id inválido"}
    rc = await db.fetchrow(
        """
        SELECT account_id, amount, category_id, name
        FROM recurring_charge WHERE recurring_charge_id = $1 AND family_id = $2
        """,
        rid, family_id,
    )
    if rc is None:
        return {"ok": False, "error": "no existe ese recurrente"}
    tx_date = _parse_date(transaction_date) or date_cls.today()
    amt = _parse_amount(amount) if amount is not None else rc["amount"]
    if amt is None:
        return {"ok": False, "error": "monto inválido (debe ser > 0)"}

    month_start = tx_date.replace(day=1)
    already = await db.fetchval(
        """
        SELECT 1 FROM transaction
        WHERE recurring_charge_id = $1 AND family_id = $2
          AND deleted_ts IS NULL AND transaction_date >= $3
        LIMIT 1
        """,
        rid, family_id, month_start,
    )
    try:
        tx_id = await db.fetchval(
            """
            INSERT INTO transaction
                (family_id, account_id, member_id, recurring_charge_id, category_id,
                 kind, amount, description, transaction_date, value_date, source)
            VALUES ($1, $2, $3, $4, $5, 'expense', $6, $7, $8, $8, 'recurring')
            RETURNING transaction_id
            """,
            family_id, rc["account_id"], member_id, rid, rc["category_id"],
            amt, rc["name"], tx_date,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pay_recurring failed: %s", exc)
        return {"ok": False, "error": "no se pudo registrar el pago"}
    return {
        "ok": True,
        "transaction_id": str(tx_id),
        "already_paid_this_month": already is not None,
    }
