"""
QueryPilot - Safety Layer Tests
Run:  python scripts/test_safety.py

These tests prove the agent can never write to or damage the database —
the thing every interviewer will ask about first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agent import validate_sql, UnsafeQueryError

SHOULD_PASS = [
    "SELECT * FROM customers",
    "select name, city from customers where kyc_verified = 1",
    "WITH t AS (SELECT account_id, COUNT(*) c FROM transactions GROUP BY 1) SELECT * FROM t",
    "SELECT COUNT(*) AS n FROM transactions WHERE status = 'failed'",
    "SELECT * FROM customers ORDER BY created_at DESC LIMIT 10;",
]

SHOULD_FAIL = [
    "DROP TABLE customers",
    "DELETE FROM transactions WHERE 1=1",
    "UPDATE accounts SET balance = 999999",
    "INSERT INTO customers VALUES (1,'x','x','x','x',1,'low','2024-01-01')",
    "SELECT * FROM customers; DROP TABLE customers",          # stacked statements
    "PRAGMA writable_schema = ON",
    "CREATE TABLE evil (x)",
    "ALTER TABLE customers ADD COLUMN hacked TEXT",
    "select * from customers; delete from accounts",          # lowercase stacking
    "",                                                        # empty
]


def run():
    passed = failed = 0

    for sql in SHOULD_PASS:
        try:
            validate_sql(sql)
            print(f"PASS (allowed)  {sql[:60]}")
            passed += 1
        except UnsafeQueryError as e:
            print(f"FAIL (should be allowed but blocked: {e})  {sql[:60]}")
            failed += 1

    for sql in SHOULD_FAIL:
        try:
            validate_sql(sql)
            print(f"FAIL (should be BLOCKED but passed!)  {sql[:60]}")
            failed += 1
        except UnsafeQueryError:
            print(f"PASS (blocked)  {sql[:60] or '<empty>'}")
            passed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    run()
