"""
fixtures/seed.py — Seeds the Neon demo database with the ACTUAL live state.

This creates the real schema that Touchstone will inspect, with intentional
drift from the declared state in touchstone-demo-target-repo/models.py:

  Live DB state (after seed):
    · users.email     VARCHAR(50)   — models.py declares VARCHAR(255)  → TYPE_CHANGED (widening)
    · users.age       BIGINT        — models.py declares INTEGER        → TYPE_CHANGED (narrowing)
    · ix_orders_user_id  MISSING    — models.py declares it             → INDEX_MISSING

Usage:
    python fixtures/seed.py <DATABASE_URL>
    # or if DATABASE_URL is in .env:
    python -c "import dotenv,os; dotenv.load_dotenv(); import fixtures.seed; fixtures.seed.main(os.environ['DATABASE_URL'])"
"""

from __future__ import annotations

import sys

import psycopg


def main(dsn: str) -> None:
    conn = psycopg.connect(dsn)
    try:
        # Drop in dependency order
        conn.execute("DROP TABLE IF EXISTS orders CASCADE")
        conn.execute("DROP TABLE IF EXISTS products CASCADE")
        conn.execute("DROP TABLE IF EXISTS users CASCADE")

        # users: email is VARCHAR(50) (narrow), age starts INTEGER then ALTERed to BIGINT
        conn.execute("""
            CREATE TABLE users (
                id         SERIAL PRIMARY KEY,
                email      VARCHAR(50) NOT NULL UNIQUE,
                age        INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # ix_users_email exists in live DB (not a drift case)
        conn.execute("CREATE INDEX ix_users_email ON users (email)")

        # orders: ix_orders_user_id intentionally NOT created (this is the ghost)
        conn.execute("""
            CREATE TABLE orders (
                id      SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                total   NUMERIC(10, 2),
                status  VARCHAR(20)
            )
        """)

        conn.execute("""
            CREATE TABLE products (
                id    SERIAL PRIMARY KEY,
                name  VARCHAR(100) NOT NULL,
                price DECIMAL(10, 2),
                stock INTEGER
            )
        """)

        # Seed data
        conn.execute("""
            INSERT INTO users (email, age) VALUES
              ('alice@example.com', 25),
              ('bob@example.com', 30),
              ('charlie@example.com', 35)
        """)
        conn.execute("""
            INSERT INTO orders (user_id, total, status) VALUES
              (1, 99.99, 'pending'),
              (1, 149.50, 'shipped'),
              (2, 29.99, 'delivered')
        """)
        conn.execute("""
            INSERT INTO products (name, price, stock) VALUES
              ('Widget', 29.99, 100),
              ('Gadget', 49.99, 50),
              ('Gizmo', 19.99, 75)
        """)

        # Widen age to BIGINT — now live DB has BIGINT, models.py still declares INTEGER
        conn.execute("ALTER TABLE users ALTER COLUMN age TYPE BIGINT")

        conn.commit()
        print("Seed complete.")
        print("  Drift cases present:")
        print("    · ix_orders_user_id  MISSING    (INDEX_MISSING, auto-fixable)")
        print(
            "    · users.email        VARCHAR(50) vs declared VARCHAR(255)  (TYPE_CHANGED, widening)"
        )
        print(
            "    · users.age          BIGINT      vs declared INTEGER        (TYPE_CHANGED, narrowing)"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <DATABASE_URL>")
        sys.exit(1)
    main(sys.argv[1])
