"""Observer agent — proactive notification generators.

Six functions, one per event type. Each:
1. Queries the DB to detect the condition.
2. Filters by per-member `notification_preference.enabled`.
3. Inserts rows into `notification` with a `dedupe_key`, using
   `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` so re-running is a no-op.

The dispatcher (`services/notifications.py`) reads those rows and sends them
via the Observer bot. We never call Telegram from here directly — keeping
generation and delivery split means a Gemini hiccup in the weekly summary
doesn't lose an already-saved row, and tests can read `notification` to
check what *would* be sent without flooding a real chat.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import AsyncSessionLocal
from app.models import (
    FamilyMember,
    Notification,
    NotificationPreference,
    Transaction,
    TransactionKind,
)
from app.services import analytics
from app.services.recurring import list_recurring_charges
from app.services.transactions import build_household_context, format_context_for_prompt

log = logging.getLogger("observer")


# ============================================================
# Templates (parametrised strings — no LLM, deterministic).
# Use simple plain text; emojis prefix gives quick scan in Telegram.
# ============================================================

TMPL_BUDGET_80 = (
    "⚠️ Presupuesto de {cat} al {pct:.0f}% del mes "
    "({spent:.0f} de {limit:.0f} EUR). Quedan {remaining:.0f} EUR."
)
TMPL_BUDGET_100 = (
    "🚨 Presupuesto de {cat} excedido — gastaste {spent:.0f} de {limit:.0f} EUR "
    "({over:.0f} EUR por encima)."
)
TMPL_RECURRING_3D = "📅 En 3 días vence {name}: {amount:.0f} EUR (día {day})."
TMPL_RECURRING_TODAY = "📅 Hoy vence {name}: {amount:.0f} EUR."
TMPL_RECURRING_OVERDUE_1 = (
    "🔔 {name} ({amount:.0f} EUR) venció ayer y no se registró el pago. "
    "¿Ya lo pagaste?"
)
TMPL_RECURRING_OVERDUE_7 = (
    "⏰ {name} ({amount:.0f} EUR) venció hace una semana y sigue sin registrarse. "
    "Revisalo cuando puedas."
)
TMPL_INACTIVITY = (
    "💭 Hace {days} días que no cargás transacciones. ¿Te olvidaste de registrar algo?"
)
TMPL_UNUSUAL = (
    "👀 Gasto inusual en {category}: {amount:.0f} EUR ({desc}). "
    "Casi {ratio:.1f}× el promedio de los últimos 90 días en esa categoría "
    "({avg:.0f} EUR)."
)

# Configurable thresholds — pulled from env so we can tune without rebuild.
BUDGET_WARN_PCT = float(os.environ.get("OBSERVER_BUDGET_WARN_PCT", "80"))
INACTIVITY_DAYS = int(os.environ.get("OBSERVER_INACTIVITY_DAYS", "2"))
UNUSUAL_TX_RATIO = float(os.environ.get("OBSERVER_UNUSUAL_TX_RATIO", "2.0"))
UNUSUAL_MIN_PEERS = int(os.environ.get("OBSERVER_UNUSUAL_MIN_PEERS", "3"))


# ============================================================
# Helpers
# ============================================================


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _year_month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _year_week(d: date) -> str:
    """ISO year + week (e.g. '2026-W21')."""
    y, w, _ = d.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _resolve_chat_id(fm: FamilyMember) -> int | None:
    """Prefer the explicit chat_id (captured at /start); fall back to
    telegram_user_id, which in 1-to-1 Telegram chats is the same number."""
    return fm.telegram_chat_id if fm.telegram_chat_id else fm.telegram_user_id


async def _enabled_members(session, kind: str) -> list[FamilyMember]:
    """Members with a Telegram bind whose preference for `kind` is enabled.
    Members without a row in notification_preference default to enabled
    (matches the seed behaviour and avoids silent opt-outs)."""
    members = (await session.scalars(
        select(FamilyMember).where(
            FamilyMember.is_active,
            FamilyMember.telegram_user_id.is_not(None),
        )
    )).all()
    out: list[FamilyMember] = []
    for fm in members:
        pref = await session.scalar(select(NotificationPreference).where(
            NotificationPreference.family_member_id == fm.family_member_id,
            NotificationPreference.kind == kind,
        ))
        if pref is None or pref.enabled:
            out.append(fm)
    return out


async def _enqueue(
    session,
    *,
    target_chat_id: int,
    kind: str,
    title: str,
    body: str,
    dedupe_key: str,
    scheduled_ts: datetime | None = None,
    related_entity_type: str | None = None,
    related_entity_id=None,
) -> bool:
    """Insert into `notification` with ON CONFLICT (dedupe_key) DO NOTHING.
    Returns True iff a new row was actually written."""
    stmt = pg_insert(Notification).values(
        target_chat_id=target_chat_id,
        kind=kind,
        title=title,
        body=body,
        scheduled_ts=scheduled_ts or _now_utc(),
        dedupe_key=dedupe_key,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"])
    result = await session.execute(stmt)
    return (result.rowcount or 0) > 0


# ============================================================
# Generators
# ============================================================


async def generate_budget_alerts() -> int:
    """Walk every monthly_budget; if spent ≥ BUDGET_WARN_PCT% (default 80),
    enqueue a one-time `budget_80` notif per (category, month). Same for 100%.

    Dedupe keys: `budget_80:{cat}:{YYYY-MM}` and `budget_100:{cat}:{YYYY-MM}`
    — at most one of each per category per month, regardless of how many
    times the scheduler fires."""
    today = date.today()
    ym = _year_month(today)
    rows = await analytics.budget_status(year_month=ym)
    if not rows:
        return 0

    enqueued = 0
    async with AsyncSessionLocal() as session:
        members_80 = await _enabled_members(session, "budget_80")
        members_100 = await _enabled_members(session, "budget_100")

        for r in rows:
            pct = r.get("pct_used") or 0.0
            cat = r["category"]
            spent = r["spent"]
            limit = r["limit"]

            if pct >= 100:
                for fm in members_100:
                    chat = _resolve_chat_id(fm)
                    if chat is None:
                        continue
                    body = TMPL_BUDGET_100.format(
                        cat=cat, spent=spent, limit=limit, over=spent - limit
                    )
                    if await _enqueue(
                        session,
                        target_chat_id=chat,
                        kind="budget_100",
                        title=f"Presupuesto {cat} excedido",
                        body=body,
                        dedupe_key=f"budget_100:{cat}:{ym}:{chat}",
                    ):
                        enqueued += 1
            elif pct >= BUDGET_WARN_PCT:
                for fm in members_80:
                    chat = _resolve_chat_id(fm)
                    if chat is None:
                        continue
                    body = TMPL_BUDGET_80.format(
                        cat=cat, pct=pct, spent=spent, limit=limit,
                        remaining=r.get("remaining", limit - spent),
                    )
                    if await _enqueue(
                        session,
                        target_chat_id=chat,
                        kind="budget_80",
                        title=f"Presupuesto {cat} al {pct:.0f}%",
                        body=body,
                        dedupe_key=f"budget_80:{cat}:{ym}:{chat}",
                    ):
                        enqueued += 1
        await session.commit()
    log.info("generate_budget_alerts: enqueued=%d", enqueued)
    return enqueued


async def generate_recurring_reminders() -> int:
    """For each active recurring_charge NOT yet paid this month, alert
    when its day_of_month is 3 days away or today. Dedupe per (rc, month)
    so each due date triggers at most one of each."""
    today = date.today()
    ym = _year_month(today)
    charges = await list_recurring_charges(include_inactive=False)
    if not charges:
        return 0

    enqueued = 0
    async with AsyncSessionLocal() as session:
        members_3d = await _enabled_members(session, "recurring_due_3d")
        members_today = await _enabled_members(session, "recurring_due_today")

        for c in charges:
            if c.get("paid_this_month"):
                continue
            try:
                due = date(today.year, today.month, c["day_of_month"])
            except ValueError:
                # day_of_month doesn't fit this month (e.g. 31 in Feb) — skip.
                continue
            days_until = (due - today).days

            if days_until == 3:
                kind = "recurring_due_3d"
                tmpl = TMPL_RECURRING_3D
                targets = members_3d
            elif days_until == 0:
                kind = "recurring_due_today"
                tmpl = TMPL_RECURRING_TODAY
                targets = members_today
            else:
                continue

            body = tmpl.format(name=c["name"], amount=c["amount"], day=c["day_of_month"])
            for fm in targets:
                chat = _resolve_chat_id(fm)
                if chat is None:
                    continue
                if await _enqueue(
                    session,
                    target_chat_id=chat,
                    kind=kind,
                    title=f"Recurrente: {c['name']}",
                    body=body,
                    dedupe_key=f"{kind}:{c['id']}:{ym}:{chat}",
                    related_entity_type="recurring_charge",
                    related_entity_id=c["id"],
                ):
                    enqueued += 1
        await session.commit()
    log.info("generate_recurring_reminders: enqueued=%d", enqueued)
    return enqueued


async def generate_recurring_overdue() -> int:
    """For each active recurring_charge whose day_of_month already passed
    this month and STILL hasn't been paid: alert 1 day after and 7 days
    after. After 7 days we stop — assume the user knows or won't pay."""
    today = date.today()
    ym = _year_month(today)
    charges = await list_recurring_charges(include_inactive=False)
    if not charges:
        return 0

    enqueued = 0
    async with AsyncSessionLocal() as session:
        members_1d = await _enabled_members(session, "recurring_overdue_1d")
        members_7d = await _enabled_members(session, "recurring_overdue_7d")

        for c in charges:
            if c.get("paid_this_month"):
                continue
            try:
                due = date(today.year, today.month, c["day_of_month"])
            except ValueError:
                continue
            overdue_days = (today - due).days

            if overdue_days == 1:
                kind = "recurring_overdue_1d"
                tmpl = TMPL_RECURRING_OVERDUE_1
                targets = members_1d
            elif overdue_days == 7:
                kind = "recurring_overdue_7d"
                tmpl = TMPL_RECURRING_OVERDUE_7
                targets = members_7d
            else:
                continue

            body = tmpl.format(name=c["name"], amount=c["amount"], day=c["day_of_month"])
            for fm in targets:
                chat = _resolve_chat_id(fm)
                if chat is None:
                    continue
                if await _enqueue(
                    session,
                    target_chat_id=chat,
                    kind=kind,
                    title=f"Vencido: {c['name']}",
                    body=body,
                    dedupe_key=f"{kind}:{c['id']}:{ym}:{chat}",
                    related_entity_type="recurring_charge",
                    related_entity_id=c["id"],
                ):
                    enqueued += 1
        await session.commit()
    log.info("generate_recurring_overdue: enqueued=%d", enqueued)
    return enqueued


async def generate_inactivity_alerts() -> int:
    """If a member hasn't registered any transaction in ≥ INACTIVITY_DAYS,
    nudge them — at most once per ISO week (dedupe by year-week) to avoid
    spamming people who genuinely have nothing to log."""
    today = date.today()
    yw = _year_week(today)

    enqueued = 0
    async with AsyncSessionLocal() as session:
        members = await _enabled_members(session, "inactivity")

        for fm in members:
            chat = _resolve_chat_id(fm)
            if chat is None:
                continue
            last = await session.scalar(
                select(func.max(Transaction.transaction_date)).where(
                    Transaction.family_member_id == fm.family_member_id,
                    Transaction.deleted_ts.is_(None),
                )
            )
            if last is None:
                # Never registered anything — probably a new member; skip
                # rather than nag from day 0.
                continue
            days = (today - last).days
            # Negative days = last transaction is in the future (e.g. a
            # future-dated entry). That's not inactivity, that's the user
            # being prepared — skip rather than send a nonsensical message.
            if days < 0 or days < INACTIVITY_DAYS:
                continue
            body = TMPL_INACTIVITY.format(days=days)
            if await _enqueue(
                session,
                target_chat_id=chat,
                kind="inactivity",
                title="¿Nada para cargar?",
                body=body,
                dedupe_key=f"inactivity:{fm.family_member_id}:{yw}",
                related_entity_type="family_member",
                related_entity_id=fm.family_member_id,
            ):
                enqueued += 1
        await session.commit()
    log.info("generate_inactivity_alerts: enqueued=%d", enqueued)
    return enqueued


async def generate_unusual_tx_alerts() -> int:
    """Scan transactions created in the last 24h. If amount > UNUSUAL_TX_RATIO ×
    average of the same category in the last 90 days (requiring at least
    UNUSUAL_MIN_PEERS peer txs to avoid noise on sparsely-used categories),
    alert. Dedupe per transaction so we never alert the same tx twice."""
    today = date.today()
    cutoff_peers = today - timedelta(days=90)

    enqueued = 0
    async with AsyncSessionLocal() as session:
        members = await _enabled_members(session, "unusual_tx")
        if not members:
            return 0
        member_ids = {fm.family_member_id: fm for fm in members}

        # Fresh expenses (created in the last 24h) from opted-in members.
        # Compare `created_ts` against NOW() server-side: the column is
        # TIMESTAMPTZ but the SQLAlchemy model declares it naive, so a
        # Python-side comparison with a tz-aware datetime trips asyncpg.
        fresh = (await session.scalars(
            select(Transaction).where(
                Transaction.deleted_ts.is_(None),
                Transaction.kind == TransactionKind.expense,
                text("transaction.created_ts >= NOW() - INTERVAL '24 hours'"),
                Transaction.family_member_id.in_(list(member_ids.keys())),
            )
        )).all()

        for tx in fresh:
            # Peer stats: same category, excluding this tx, over 90d.
            stats = (await session.execute(
                select(
                    func.avg(Transaction.amount).label("avg"),
                    func.count(Transaction.transaction_id).label("n"),
                ).where(
                    Transaction.deleted_ts.is_(None),
                    Transaction.kind == TransactionKind.expense,
                    Transaction.category == tx.category,
                    Transaction.transaction_id != tx.transaction_id,
                    Transaction.transaction_date >= cutoff_peers,
                )
            )).one()
            avg = float(stats.avg or 0)
            n = int(stats.n or 0)
            if n < UNUSUAL_MIN_PEERS or avg <= 0:
                continue
            ratio = float(tx.amount) / avg
            if ratio < UNUSUAL_TX_RATIO:
                continue

            fm = member_ids[tx.family_member_id]
            chat = _resolve_chat_id(fm)
            if chat is None:
                continue
            body = TMPL_UNUSUAL.format(
                category=tx.category,
                amount=float(tx.amount),
                desc=tx.description or "sin descripción",
                ratio=ratio,
                avg=avg,
            )
            if await _enqueue(
                session,
                target_chat_id=chat,
                kind="unusual_tx",
                title=f"Gasto inusual: {tx.category}",
                body=body,
                dedupe_key=f"unusual:{tx.transaction_id}",
                related_entity_type="transaction",
                related_entity_id=tx.transaction_id,
            ):
                enqueued += 1
        await session.commit()
    log.info("generate_unusual_tx_alerts: enqueued=%d", enqueued)
    return enqueued


# ============================================================
# Weekly summary — the one notif that benefits from an LLM
# ============================================================


_WEEKLY_PROMPT = (
    "Sos un analista financiero familiar. A partir de los datos JSON de "
    "abajo, redactá un RESUMEN SEMANAL para la persona (en español, tono "
    "amable y profesional). Formato: 4-6 bullets cortos con los números "
    "más relevantes. NO inventes datos; si un campo está vacío, no lo "
    "menciones. Empezá con una línea de saludo + neto de la semana.\n\n"
    "{ctx}\n\n"
    "Datos:\n{data}"
)


async def _llm_weekly_text(member_name: str, payload: dict, ctx_text: str) -> str:
    """Call Gemini once to compose the weekly summary body. On any failure,
    fall back to a deterministic plain-text rendering of the same payload —
    we never silently skip the alert just because the LLM is unhappy."""
    import json as _json

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        # Use only one model here (no rotation needed; if Gemini is fully
        # down, fallback covers it).
        from app.services.agent import MODELS
        prompt = _WEEKLY_PROMPT.format(ctx=ctx_text, data=_json.dumps(payload, indent=2))
        resp = await client.aio.models.generate_content(
            model=MODELS[0] if MODELS else "gemini-2.5-flash",
            contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0.3),
        )
        text = (resp.text or "").strip()
        if text:
            return text
    except Exception:
        log.exception("weekly summary LLM failed; falling back to template")

    # Deterministic fallback — uglier but always works.
    bal = payload.get("balance", {})
    lines = [
        f"Hola {member_name}, resumen de la semana:",
        f"• Ingresos: {bal.get('income', 0):.0f} EUR",
        f"• Gastos: {bal.get('expense', 0):.0f} EUR",
        f"• Neto: {bal.get('net', 0):.0f} EUR",
    ]
    if payload.get("top_categories"):
        lines.append("• Top gastos: " + ", ".join(
            f"{c['category']} {c['total']:.0f}" for c in payload["top_categories"][:3]
        ))
    cmp = payload.get("vs_previous_week", {})
    if cmp.get("pct_change") is not None:
        lines.append(f"• Vs semana anterior: {cmp['pct_change']:+.1f}% en gasto")
    return "\n".join(lines)


async def generate_weekly_summary() -> int:
    """Sunday-evening report per member. Compute the numbers once with the
    analytics service, hand them to the LLM for prose, persist as a single
    notif per member per ISO week. Idempotent thanks to the dedupe key."""
    today = date.today()
    yw = _year_week(today)
    # Week boundaries: last 7 days ending today (cron fires Sunday).
    end = today
    start = end - timedelta(days=6)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)

    enqueued = 0
    ctx = await build_household_context()
    ctx_text = format_context_for_prompt(ctx)

    async with AsyncSessionLocal() as session:
        members = await _enabled_members(session, "weekly_summary")
        if not members:
            return 0

        # Compute the heavy stuff once per week; reuse across members.
        balance = await analytics.balance_for_period(start.isoformat(), end.isoformat())
        tops = await analytics.top_categories(start.isoformat(), end.isoformat(),
                                              kind="expense", limit=5)
        cmp = await analytics.period_comparison(
            (prev_start.isoformat(), prev_end.isoformat()),
            (start.isoformat(), end.isoformat()),
            kind="expense",
        )
        budgets = await analytics.budget_status(year_month=_year_month(today))
        # Surface only budgets at risk (≥ 80%) so the LLM doesn't drown in noise.
        budgets_risky = [b for b in budgets if (b.get("pct_used") or 0) >= 80]

        payload = {
            "week": f"{start.isoformat()} → {end.isoformat()}",
            "balance": balance,
            "top_categories": tops,
            "vs_previous_week": cmp,
            "budgets_at_risk": budgets_risky,
        }

        for fm in members:
            chat = _resolve_chat_id(fm)
            if chat is None:
                continue
            body = await _llm_weekly_text(fm.full_name, payload, ctx_text)
            if await _enqueue(
                session,
                target_chat_id=chat,
                kind="weekly_summary",
                title=f"Resumen semanal ({start.strftime('%d/%m')}–{end.strftime('%d/%m')})",
                body=body,
                dedupe_key=f"weekly:{yw}:{fm.family_member_id}",
                related_entity_type="family_member",
                related_entity_id=fm.family_member_id,
            ):
                enqueued += 1
        await session.commit()
    log.info("generate_weekly_summary: enqueued=%d", enqueued)
    return enqueued
