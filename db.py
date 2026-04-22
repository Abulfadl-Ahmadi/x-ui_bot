import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).resolve().parent / "bot.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_def: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_def}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


def _normalize_subscription_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return "special" if normalized == "special" else "normal"


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                referral TEXT,
                admin_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                volume INTEGER NOT NULL,
                status TEXT NOT NULL,
                receipt_file_id TEXT,
                receipt_local_path TEXT,
                vpn_link TEXT,
                sub_link TEXT,
                admin_id INTEGER,
                referral_code TEXT,
                discount_code TEXT,
                discount_percent INTEGER,
                final_price INTEGER,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_codes (
                code TEXT PRIMARY KEY,
                admin_id INTEGER,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_settings (
                admin_id INTEGER PRIMARY KEY,
                sales_open INTEGER NOT NULL DEFAULT 1,
                subscription_source_mode TEXT NOT NULL DEFAULT 'xui',
                updated_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_subscription_sales (
                admin_id INTEGER NOT NULL,
                subscription_type TEXT NOT NULL,
                sales_open INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL,
                PRIMARY KEY (admin_id, subscription_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscription_inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                subscription_type TEXT NOT NULL,
                price INTEGER NOT NULL,
                link TEXT NOT NULL UNIQUE,
                config_link TEXT,
                sub_link TEXT,
                is_used INTEGER NOT NULL DEFAULT 0,
                used_order_id TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discount_codes (
                code TEXT PRIMARY KEY,
                admin_id INTEGER NOT NULL,
                percent INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL
            )
            """
        )
        _ensure_column(conn, "users", "admin_id INTEGER")
        _ensure_column(conn, "users", "terms_accepted INTEGER DEFAULT 0")
        _ensure_column(conn, "orders", "admin_id INTEGER")
        _ensure_column(conn, "orders", "referral_code TEXT")
        _ensure_column(conn, "orders", "discount_code TEXT")
        _ensure_column(conn, "orders", "discount_percent INTEGER")
        _ensure_column(conn, "orders", "subscription_type TEXT")
        _ensure_column(conn, "orders", "subscription_source_mode TEXT DEFAULT 'xui'")
        _ensure_column(conn, "orders", "base_price INTEGER")
        _ensure_column(conn, "orders", "final_price INTEGER")
        _ensure_column(conn, "orders", "sub_link TEXT")
        _ensure_column(conn, "orders", "receipt_local_path TEXT")
        _ensure_column(conn, "referral_codes", "admin_id INTEGER")
        _ensure_column(conn, "admin_settings", "subscription_source_mode TEXT DEFAULT 'xui'")
        _ensure_column(conn, "subscription_inventory", "config_link TEXT")
        _ensure_column(conn, "subscription_inventory", "sub_link TEXT")


def ensure_admin_exists(admin_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO admin_settings(admin_id, sales_open, updated_at)
            VALUES(?, 1, ?)
            ON CONFLICT(admin_id) DO NOTHING
            """,
            (admin_id, datetime.utcnow().isoformat()),
        )


def set_admin_sales_open(admin_id: int, is_open: bool) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO admin_settings(admin_id, sales_open, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                sales_open = excluded.sales_open,
                updated_at = excluded.updated_at
            """,
            (admin_id, 1 if is_open else 0, datetime.utcnow().isoformat()),
        )


def set_admin_subscription_type_sales_open(admin_id: int, subscription_type: str, is_open: bool) -> None:
    normalized_type = _normalize_subscription_type(subscription_type)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO admin_subscription_sales(admin_id, subscription_type, sales_open, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(admin_id, subscription_type) DO UPDATE SET
                sales_open = excluded.sales_open,
                updated_at = excluded.updated_at
            """,
            (admin_id, normalized_type, 1 if is_open else 0, datetime.utcnow().isoformat()),
        )


def is_admin_subscription_type_sales_open(admin_id: int, subscription_type: str) -> bool:
    normalized_type = _normalize_subscription_type(subscription_type)
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT sales_open
            FROM admin_subscription_sales
            WHERE admin_id = ? AND subscription_type = ?
            """,
            (admin_id, normalized_type),
        ).fetchone()
        if row is None:
            return True
        return bool(row["sales_open"])


def get_admin_subscription_sales_status(admin_id: int) -> dict[str, bool]:
    status = {"normal": True, "special": True}
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT subscription_type, sales_open
            FROM admin_subscription_sales
            WHERE admin_id = ?
            """,
            (admin_id,),
        ).fetchall()
        for row in rows:
            subscription_type = _normalize_subscription_type(row["subscription_type"])
            status[subscription_type] = bool(row["sales_open"])
    return status


def is_admin_sales_open(admin_id: int) -> bool:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT sales_open FROM admin_settings WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()
        if row is None:
            return True
        return bool(row["sales_open"])


def get_admin_subscription_source_mode(admin_id: int) -> str:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT subscription_source_mode FROM admin_settings WHERE admin_id = ?",
            (admin_id,),
        ).fetchone()
        if row is None or not row["subscription_source_mode"]:
            return "xui"
        return str(row["subscription_source_mode"])


def set_admin_subscription_source_mode(admin_id: int, mode: str) -> None:
    normalized = "file" if str(mode).strip().lower() == "file" else "xui"
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO admin_settings(admin_id, sales_open, subscription_source_mode, updated_at)
            VALUES(?, 1, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                subscription_source_mode = excluded.subscription_source_mode,
                updated_at = excluded.updated_at
            """,
            (admin_id, normalized, datetime.utcnow().isoformat()),
        )


def save_user(user_id: int, referral: str, admin_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users(user_id, referral, admin_id)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                referral = excluded.referral,
                admin_id = excluded.admin_id
            """,
            (user_id, referral, admin_id),
        )


def sync_subscription_inventory(admin_id: int, records: list[dict[str, object]]) -> int:
    inserted = 0
    with _get_conn() as conn:
        for record in records:
            subscription_type = str(record.get("subscription_type") or "normal")
            price = int(record.get("price") or 0)
            config_link = str(record.get("config_link") or record.get("link") or "").strip()
            sub_link = str(record.get("sub_link") or "").strip() or None
            if not config_link:
                continue

            # File-based inventory is shared between all admins.
            shared_admin_id = 0

            cursor = conn.execute(
                """
                INSERT INTO subscription_inventory(
                    admin_id, subscription_type, price, link, config_link, sub_link,
                    is_used, used_order_id, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, 0, NULL, ?, ?)
                ON CONFLICT(link) DO UPDATE SET
                    admin_id = excluded.admin_id,
                    subscription_type = excluded.subscription_type,
                    price = excluded.price,
                    config_link = excluded.config_link,
                    sub_link = excluded.sub_link,
                    updated_at = excluded.updated_at
                """,
                (
                    shared_admin_id,
                    subscription_type,
                    price,
                    config_link,
                    config_link,
                    sub_link,
                    datetime.utcnow().isoformat(),
                    datetime.utcnow().isoformat(),
                ),
            )
            inserted += 1 if cursor.rowcount else 0
    return inserted


def peek_available_subscription(admin_id: int, subscription_type: str) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT id, admin_id, subscription_type, price,
                   COALESCE(config_link, link) AS config_link,
                   sub_link,
                   is_used, used_order_id, created_at, updated_at
            FROM subscription_inventory
            WHERE subscription_type = ? AND is_used = 0
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (subscription_type,),
        ).fetchone()


def claim_subscription_for_order(order_id: str, admin_id: int, subscription_type: str) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, admin_id, subscription_type, price,
                   COALESCE(config_link, link) AS config_link,
                   sub_link
            FROM subscription_inventory
            WHERE subscription_type = ? AND is_used = 0
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """,
            (subscription_type,),
        ).fetchone()
        if row is None:
            return None

        conn.execute(
            """
            UPDATE subscription_inventory
            SET is_used = 1,
                used_order_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (order_id, datetime.utcnow().isoformat(), row["id"]),
        )
        return row


def release_subscription_for_order(order_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE subscription_inventory
            SET is_used = 0,
                used_order_id = NULL,
                updated_at = ?
            WHERE used_order_id = ?
            """,
            (datetime.utcnow().isoformat(), order_id),
        )


def add_referral_codes(codes: list[str], admin_id: int) -> int:
    inserted = 0
    with _get_conn() as conn:
        for code in codes:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO referral_codes(code, admin_id, created_at)
                VALUES(?, ?, ?)
                """,
                (code, admin_id, datetime.utcnow().isoformat()),
            )
            inserted += cursor.rowcount
    return inserted


def is_referral_code_valid(code: str) -> bool:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM referral_codes WHERE code = ? LIMIT 1",
            (code,),
        ).fetchone()
        return row is not None


def list_referral_codes() -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT code FROM referral_codes ORDER BY created_at DESC"
        ).fetchall()
        return [row["code"] for row in rows]


def get_referral_code_owner(code: str) -> Optional[int]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT admin_id FROM referral_codes WHERE code = ? LIMIT 1",
            (code,),
        ).fetchone()
        if row is None:
            return None
        return row["admin_id"]


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            "SELECT user_id, referral, admin_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def accept_terms(user_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET terms_accepted = 1 WHERE user_id = ?",
            (user_id,),
        )


def user_accepted_terms(user_id: int) -> bool:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT terms_accepted FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return False
        return bool(row["terms_accepted"])


def has_pending_order(user_id: int) -> bool:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE user_id = ? AND status = 'pending' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None


def create_order(
    order_id: str,
    user_id: int,
    volume: int,
    admin_id: int,
    referral_code: str,
    subscription_type: str,
    subscription_source_mode: str,
    base_price: int,
    discount_code: Optional[str],
    discount_percent: int,
    final_price: int,
) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO orders(
                id, user_id, volume, status, receipt_file_id, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            )
            VALUES(?, ?, ?, 'pending', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                user_id,
                volume,
                admin_id,
                referral_code,
                subscription_type,
                subscription_source_mode,
                base_price,
                discount_code,
                discount_percent,
                final_price,
                datetime.utcnow().isoformat(),
            ),
        )


def get_latest_pending_order(user_id: int) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT
                id, user_id, volume, status, receipt_file_id, receipt_local_path, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            FROM orders
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def save_receipt(order_id: str, receipt_file_id: str, receipt_local_path: Optional[str] = None) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET receipt_file_id = ?, receipt_local_path = ? WHERE id = ?",
            (receipt_file_id, receipt_local_path, order_id),
        )


def get_order(order_id: str) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT
                id, user_id, volume, status, receipt_file_id, receipt_local_path, vpn_link, sub_link, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()


def approve_order(order_id: str, vpn_link: str, sub_link: Optional[str] = None) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = 'approved', vpn_link = ?, sub_link = ? WHERE id = ?",
            (vpn_link, sub_link, order_id),
        )


def cancel_order(order_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = 'cancelled' WHERE id = ?",
            (order_id,),
        )


def reject_order(order_id: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE orders SET status = 'rejected' WHERE id = ?",
            (order_id,),
        )


def get_latest_approved_order(user_id: int) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT
                id, user_id, volume, status, vpn_link, sub_link, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            FROM orders
            WHERE user_id = ? AND status = 'approved' AND vpn_link IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()


def get_all_approved_orders(user_id: int) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                id, user_id, volume, status, vpn_link, sub_link, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            FROM orders
            WHERE user_id = ? AND status = 'approved' AND vpn_link IS NOT NULL
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return rows


def get_pending_orders_by_admin(admin_id: int) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                id, user_id, volume, status, receipt_file_id, receipt_local_path, vpn_link, sub_link, admin_id,
                referral_code, subscription_type, subscription_source_mode, base_price,
                discount_code, discount_percent, final_price, created_at
            FROM orders
            WHERE admin_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (admin_id,),
        ).fetchall()
        return rows


def create_discount_code(code: str, admin_id: int, percent: int, max_uses: int) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO discount_codes(
                code, admin_id, percent, max_uses, used_count, is_active, created_at
            )
            VALUES(?, ?, ?, ?, 0, 1, ?)
            """,
            (code, admin_id, percent, max_uses, datetime.utcnow().isoformat()),
        )
        return cursor.rowcount == 1


def list_discount_codes(admin_id: int) -> list[sqlite3.Row]:
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT code, percent, max_uses, used_count, is_active, created_at
            FROM discount_codes
            WHERE admin_id = ?
            ORDER BY created_at DESC
            """,
            (admin_id,),
        ).fetchall()


def deactivate_discount_code(code: str, admin_id: int) -> str:
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT is_active
            FROM discount_codes
            WHERE code = ? AND admin_id = ?
            LIMIT 1
            """,
            (code, admin_id),
        ).fetchone()

        if row is None:
            return "not_found"

        if int(row["is_active"]) == 0:
            return "already_inactive"

        conn.execute(
            """
            UPDATE discount_codes
            SET is_active = 0
            WHERE code = ? AND admin_id = ?
            """,
            (code, admin_id),
        )
        return "deactivated"


def delete_discount_code(code: str, admin_id: int) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            DELETE FROM discount_codes
            WHERE code = ? AND admin_id = ?
            """,
            (code, admin_id),
        )
        return cursor.rowcount > 0


def validate_discount_code(code: str, admin_id: int) -> Optional[sqlite3.Row]:
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT code, admin_id, percent, max_uses, used_count, is_active
            FROM discount_codes
            WHERE code = ? AND admin_id = ?
            LIMIT 1
            """,
            (code, admin_id),
        ).fetchone()
        if row is None:
            return None
        if not bool(row["is_active"]):
            return None
        if int(row["used_count"]) >= int(row["max_uses"]):
            return None
        return row


def consume_discount_code(code: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            """
            UPDATE discount_codes
            SET used_count = used_count + 1
            WHERE code = ?
            """,
            (code,),
        )
