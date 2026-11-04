"""Apply Postgres migrations as the schema owner.

    MIGRATION_DATABASE_URL=postgresql://postgres...  python -m app.migrate

Used by the manual "db-migrate" GitHub Action (owner credentials live only in GitHub
secrets, never on the Render server).
"""
import os
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "migrations" / "postgres"


def main():
    url = os.getenv("MIGRATION_DATABASE_URL", "").strip()
    if not url:
        sys.exit("MIGRATION_DATABASE_URL is not set")
    import psycopg2
    conn = psycopg2.connect(url, connect_timeout=20)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("CREATE SCHEMA IF NOT EXISTS margin")
    cur.execute("CREATE TABLE IF NOT EXISTS margin.schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    cur.execute("SELECT version FROM margin.schema_migrations")
    done = {r[0] for r in cur.fetchall()}
    for f in sorted(DIR.glob("*.sql")):
        if f.stem in done:
            print(f"skip  {f.name}")
            continue
        print(f"apply {f.name}")
        cur.execute(f.read_text())
    conn.commit()
    print("migrations up to date")


if __name__ == "__main__":
    main()
