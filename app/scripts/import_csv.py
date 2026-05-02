"""
Script de importación del CSV histórico.
Ejecutar dentro del contenedor:
  docker exec sovereignbox_worker python3 app/scripts/import_csv.py
"""
import csv
import os
import sys
import uuid
from datetime import datetime

import psycopg2

CSV_PATH = "/csv/movimientos-data-base.csv"
DATABASE_URL = os.environ["DATABASE_URL"].replace("+asyncpg", "").replace("+psycopg2", "")


def parse_date(s: str):
    s = s.strip()
    for fmt in ("%d/%m/%Y",):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_amount(s: str):
    s = s.strip().replace("€", "").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def normalize_origen(s: str) -> str:
    m = {"telegram": "telegram", "whatsapp": "whatsapp",
         "computadora": "computadora", "desde excel": "importado"}
    return m.get(s.strip().lower(), "importado")


def main():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    # Obtener family_member IDs
    cur.execute("SELECT full_name, id FROM family_members WHERE is_active = true")
    members = {name: mid for name, mid in cur.fetchall()}
    print(f"Miembros encontrados: {list(members.keys())}")

    if "Hector Marioni" not in members and "Hector" not in members:
        print("ERROR: no se encontró a Hector en family_members")
        sys.exit(1)

    hector_id = members.get("Hector Marioni") or members.get("Hector")
    luisiana_id = members.get("Luisiana")

    if not luisiana_id:
        print("ERROR: no se encontró a Luisiana. Ejecutá primero la migración 003.")
        sys.exit(1)

    member_map = {"Hector": hector_id, "Luisiana": luisiana_id}

    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    imported = 0
    skipped = 0

    for r in rows:
        # Saltear eliminados y filas sin datos esenciales
        if r["estado"].strip() == "eliminado":
            skipped += 1
            continue

        tx_date = parse_date(r["fecha"])
        amount = parse_amount(r["monto"])

        if not tx_date or not amount:
            skipped += 1
            continue

        editado = r["editado_de"].strip()
        family_member_id = member_map.get(editado)
        if not family_member_id:
            # Si no matchea exacto, intentar parcial
            for name, mid in member_map.items():
                if name.lower() in editado.lower():
                    family_member_id = mid
                    break
        if not family_member_id:
            family_member_id = hector_id  # fallback

        tipo = r["tipo"].strip() or ("ingreso" if r["categoria"].strip() == "Entradas" else "gasto")
        categoria = r["categoria"].strip() or None
        sub1 = r["subcategoria_n1"].strip() or None
        sub2 = r["subcategoria_n2"].strip() or None
        sub3 = r["subcategoria_n3"].strip() or None
        nota = r["nota"].strip() or None
        origen = normalize_origen(r["origen"])

        cur.execute(
            """
            INSERT INTO transactions
                (id, family_member_id, transaction_date, tipo, amount, currency,
                 categoria, subcategoria1, subcategoria2, subcategoria3,
                 nota, origen, created_at)
            VALUES
                (%s, %s, %s, %s, %s, 'EUR',
                 %s, %s, %s, %s,
                 %s, %s, now())
            """,
            (
                str(uuid.uuid4()), str(family_member_id), tx_date, tipo, amount,
                categoria, sub1, sub2, sub3,
                nota, origen,
            ),
        )
        imported += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\n✅ Importados: {imported}")
    print(f"⏭️  Saltados:   {skipped}")


if __name__ == "__main__":
    main()
