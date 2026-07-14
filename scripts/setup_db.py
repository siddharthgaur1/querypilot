"""
QueryPilot - Database Setup
Creates a realistic fintech SQLite database with customers, accounts,
transactions, and merchants — so the agent has something real to query.

Usage:
    python scripts/setup_db.py            # create DB with 5,000 transactions
    python scripts/setup_db.py --rows 20000
"""

import argparse
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fintech.db"

FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Ananya", "Diya", "Ishaan", "Kavya",
               "Rohan", "Priya", "Arjun", "Sneha", "Karan", "Meera", "Rahul",
               "Pooja", "Siddharth", "Neha", "Vikram", "Anjali", "Amit"]
LAST_NAMES = ["Sharma", "Verma", "Gupta", "Patel", "Singh", "Kumar", "Joshi",
              "Reddy", "Nair", "Iyer", "Mehta", "Chopra", "Malhotra", "Bose"]
CITIES = ["Mumbai", "Delhi", "Bengaluru", "Chennai", "Hyderabad", "Pune",
          "Kolkata", "Ahmedabad", "Jaipur", "Lucknow"]
MERCHANT_NAMES = ["Amazon India", "Flipkart", "Swiggy", "Zomato", "BigBasket",
                  "Reliance Digital", "DMart", "Myntra", "BookMyShow", "Uber",
                  "Ola", "IRCTC", "Jio Recharge", "Airtel Payments", "Croma",
                  "Nykaa", "PharmEasy", "Zepto", "Blinkit", "MakeMyTrip"]
MERCHANT_CATEGORIES = {
    "Amazon India": "ecommerce", "Flipkart": "ecommerce", "Myntra": "ecommerce",
    "Nykaa": "ecommerce", "Swiggy": "food", "Zomato": "food", "Zepto": "grocery",
    "BigBasket": "grocery", "DMart": "grocery", "Blinkit": "grocery",
    "Reliance Digital": "electronics", "Croma": "electronics",
    "BookMyShow": "entertainment", "Uber": "travel", "Ola": "travel",
    "IRCTC": "travel", "MakeMyTrip": "travel", "Jio Recharge": "utilities",
    "Airtel Payments": "utilities", "PharmEasy": "health",
}
TXN_STATUSES = ["success", "success", "success", "success", "success",
                "success", "success", "failed", "pending", "reversed"]
FAILURE_REASONS = ["insufficient_funds", "gateway_timeout", "invalid_otp",
                   "card_declined", "network_error", "limit_exceeded"]
ACCOUNT_TYPES = ["savings", "current", "wallet"]
PAYMENT_METHODS = ["UPI", "debit_card", "credit_card", "netbanking", "wallet"]


def create_schema(conn: sqlite3.Connection):
    conn.executescript("""
    DROP TABLE IF EXISTS transactions;
    DROP TABLE IF EXISTS accounts;
    DROP TABLE IF EXISTS customers;
    DROP TABLE IF EXISTS merchants;

    CREATE TABLE customers (
        customer_id   INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        email         TEXT UNIQUE NOT NULL,
        phone         TEXT,
        city          TEXT,
        kyc_verified  INTEGER NOT NULL DEFAULT 0,      -- 0/1
        risk_segment  TEXT CHECK(risk_segment IN ('low','medium','high')),
        created_at    TEXT NOT NULL                     -- ISO date
    );

    CREATE TABLE accounts (
        account_id    INTEGER PRIMARY KEY,
        customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
        account_type  TEXT CHECK(account_type IN ('savings','current','wallet')),
        balance       REAL NOT NULL DEFAULT 0,
        is_active     INTEGER NOT NULL DEFAULT 1,
        opened_at     TEXT NOT NULL
    );

    CREATE TABLE merchants (
        merchant_id   INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        category      TEXT NOT NULL,
        city          TEXT
    );

    CREATE TABLE transactions (
        txn_id          INTEGER PRIMARY KEY,
        account_id      INTEGER NOT NULL REFERENCES accounts(account_id),
        merchant_id     INTEGER REFERENCES merchants(merchant_id),
        amount          REAL NOT NULL,
        payment_method  TEXT,
        status          TEXT CHECK(status IN ('success','failed','pending','reversed')),
        failure_reason  TEXT,                            -- NULL unless failed
        created_at      TEXT NOT NULL                    -- ISO datetime
    );

    CREATE INDEX idx_txn_account ON transactions(account_id);
    CREATE INDEX idx_txn_status  ON transactions(status);
    CREATE INDEX idx_txn_date    ON transactions(created_at);
    """)


def seed(conn: sqlite3.Connection, n_txns: int):
    rng = random.Random(42)  # reproducible
    now = datetime(2026, 6, 11)

    # Customers
    customers = []
    for cid in range(1, 401):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        email = f"{name.lower().replace(' ', '.')}{cid}@example.com"
        created = now - timedelta(days=rng.randint(30, 1200))
        customers.append((
            cid, name, email,
            f"+91 9{rng.randint(100000000, 999999999)}",
            rng.choice(CITIES),
            rng.random() > 0.12,
            rng.choices(["low", "medium", "high"], weights=[70, 22, 8])[0],
            created.date().isoformat(),
        ))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)

    # Accounts (1-2 per customer)
    accounts = []
    aid = 1
    for cid, *_rest, created in customers:
        for _ in range(rng.choice([1, 1, 1, 2])):
            accounts.append((
                aid, cid, rng.choice(ACCOUNT_TYPES),
                round(rng.uniform(0, 250000), 2),
                rng.random() > 0.05,
                created,
            ))
            aid += 1
    conn.executemany("INSERT INTO accounts VALUES (?,?,?,?,?,?)", accounts)

    # Merchants
    merchants = [(i + 1, name, MERCHANT_CATEGORIES[name], random.Random(i).choice(CITIES))
                 for i, name in enumerate(MERCHANT_NAMES)]
    conn.executemany("INSERT INTO merchants VALUES (?,?,?,?)", merchants)

    # Transactions — weighted toward recent dates, some accounts failure-prone
    failure_prone = set(rng.sample(range(1, aid), k=max(5, aid // 12)))
    txns = []
    for tid in range(1, n_txns + 1):
        account_id = rng.randint(1, aid - 1)
        days_ago = int(rng.betavariate(1.2, 3.5) * 365)  # skew recent
        ts = now - timedelta(days=days_ago,
                             hours=rng.randint(0, 23),
                             minutes=rng.randint(0, 59))
        if account_id in failure_prone and rng.random() < 0.35:
            status = "failed"
        else:
            status = rng.choice(TXN_STATUSES)
        txns.append((
            tid, account_id, rng.randint(1, len(MERCHANT_NAMES)),
            round(rng.lognormvariate(6.5, 1.1), 2),  # realistic amount distribution
            rng.choice(PAYMENT_METHODS),
            status,
            rng.choice(FAILURE_REASONS) if status == "failed" else None,
            ts.isoformat(sep=" ", timespec="seconds"),
        ))
    conn.executemany("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?)", txns)
    conn.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=5000, help="number of transactions")
    args = parser.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    create_schema(conn)
    seed(conn, args.rows)

    cur = conn.execute("SELECT COUNT(*) FROM customers")
    nc = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM accounts")
    na = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM transactions")
    nt = cur.fetchone()[0]
    conn.close()
    print(f"Created {DB_PATH}")
    print(f"  {nc} customers | {na} accounts | {len(MERCHANT_NAMES)} merchants | {nt} transactions")
