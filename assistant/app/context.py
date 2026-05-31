"""Family context for the agent's prompt.

One round-trip that reads the family's members, accounts, budgets and recurring
charges, rendered as compact Spanish so the agent knows the real structure (not
just category names). Ported from household's build_household_context /
format_context_for_prompt, scoped to one family_id.
"""

from app import db, tools


async def build_family_context(family_id) -> dict:
    members = await db.fetch(
        "SELECT full_name FROM member WHERE family_id = $1 AND is_active ORDER BY created_ts",
        family_id,
    )
    accounts = await db.fetch(
        """
        SELECT a.name, a.kind, a.currency, m.full_name AS owner
        FROM account a
        LEFT JOIN member m ON m.member_id = a.member_id
        WHERE a.family_id = $1 AND a.is_active
        ORDER BY a.created_ts
        """,
        family_id,
    )
    categories = await tools.active_category_names(family_id)
    budgets = await db.fetch(
        """
        SELECT c.name AS category, b.limit_amount AS limit
        FROM monthly_budget b
        JOIN category c ON c.category_id = b.category_id
        WHERE b.family_id = $1
        ORDER BY c.name
        """,
        family_id,
    )
    recurring = await db.fetch(
        """
        SELECT r.name, r.amount, r.day_of_month AS day, c.name AS category, a.name AS account
        FROM recurring_charge r
        JOIN category c ON c.category_id = r.category_id
        JOIN account  a ON a.account_id  = r.account_id
        WHERE r.family_id = $1 AND r.is_active
        ORDER BY r.name
        """,
        family_id,
    )
    return {
        "members": [{"name": m["full_name"]} for m in members],
        "accounts": [dict(a) for a in accounts],
        "categories": categories,
        "budgets": [{"category": b["category"], "limit": float(b["limit"])} for b in budgets],
        "recurring": [
            {"name": r["name"], "amount": float(r["amount"]), "day": r["day"],
             "category": r["category"], "account": r["account"]}
            for r in recurring
        ],
    }


def format_context_for_prompt(ctx: dict) -> str:
    """Compact Spanish rendering; empty sections are omitted so the prompt
    doesn't lie about absent data."""
    lines = ["=== Contexto familiar ==="]

    members = [m["name"] for m in ctx.get("members", [])]
    if members:
        lines.append("Miembros: " + ", ".join(members))

    accounts = ctx.get("accounts", [])
    if accounts:
        lines.append("Cuentas:")
        for a in accounts:
            owner = a.get("owner") or "compartida"
            lines.append(f"- {a['name']} ({a['kind']}, {a['currency']}) — {owner}")

    budgets = ctx.get("budgets", [])
    if budgets:
        lines.append("Presupuestos mensuales:")
        for b in budgets:
            lines.append(f"- {b['category']}: {b['limit']:g} EUR")

    recurring = ctx.get("recurring", [])
    if recurring:
        lines.append("Gastos recurrentes:")
        for r in recurring:
            lines.append(
                f"- {r['name']}: {r['amount']:g} EUR el día {r['day']} "
                f"({r['category']}, {r['account']})"
            )

    return "\n".join(lines)


def format_recent(rows: list[dict]) -> str:
    if not rows:
        return "(no hay gastos recientes)"
    return "\n".join(
        f"- {r['date']} | {r['amount']:.2f} {r['currency']} | {r['category']} | "
        f"{r['description'] or '(sin descripción)'}"
        for r in rows
    )
