# household

The next version of the family assistant — rebuilt around the 3-agent architecture (Orchestrator / Consultant / Observer) documented in the repo root `README.md`.

Status: **scaffolding**. Nothing runs yet. The working reference is `../sovereignbox/`.

## Planned layout

```
household/
├── web/                  # frontend (browser-facing)
├── server/               # backend (API + workers + agents)
├── migrations/           # SQL schema, applied in order
└── docker-compose.yml    # shared infra (Postgres, Redis, Ollama)
```

`web/` and `server/` will be added when there's code to put in them. Until then this folder is intentionally empty.

## Data model

5 tables, financial-only. Schema in `migrations/schema.sql`. Conventions: singular table names, PK + FK columns named `{table}_id`, timestamps end in `_ts`, dates end in `_date`.

`transaction` is the fact table; `account`, `family_member`, `recurring_charge` are dimensions around it. `monthly_budget` is joined by `subcategory_1`, not by FK.

### Association rules

- **Who creates what.** UI/bot creates `transaction` rows. The recurring worker creates them automatically from `recurring_charge` on each `day_of_month`. `monthly_budget` is dashboard-only.
- **FKs on `transaction`.** Always: `account_id`, `family_member_id`. Only when auto-generated: `recurring_charge_id` (else NULL).
- **Dates.** `transaction_date` = when it happened. `value_date` = when money moves. Cash/checking: equal. Credit card: `value_date` is the statement-pay date — caller is responsible for setting it.
- **Balances.** `account.current_balance = initial_balance + Σ(transaction where value_date > balance_date AND value_date <= today)`. Historical loads (`value_date <= balance_date`) don't affect current balance.
- **Soft-delete.** Only `transaction`. Every query and view must filter `WHERE deleted_ts IS NULL`.
- **Categories.** `category` always set. `subcategory_1` required for any tx you want budget-tracked. `subcategory_2` is free-form.

## Why "household"

The group of people who share a home and the daily running of it. See the suggestion-history conversation in the repo for the full picking process.
