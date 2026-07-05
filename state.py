import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol                 TEXT PRIMARY KEY,
    entry_order_id         TEXT,
    qty                    INTEGER NOT NULL,
    initial_qty            INTEGER NOT NULL,
    entry_price            REAL NOT NULL,
    entry_time             TEXT NOT NULL,
    initial_stop           REAL NOT NULL,
    current_stop           REAL NOT NULL,
    current_stop_order_id  TEXT,
    risk_per_share         REAL NOT NULL,
    partial_target_price   REAL NOT NULL,
    breakeven_price        REAL NOT NULL,
    stage                  TEXT NOT NULL CHECK(stage IN ('none','partial_done','breakeven','trailing')) DEFAULT 'none',
    last_updated           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_orders (
    order_id         TEXT PRIMARY KEY,
    symbol           TEXT NOT NULL,
    purpose          TEXT NOT NULL CHECK(purpose IN ('entry','stop','partial_exit','force_close')),
    submitted_at     TEXT NOT NULL,
    qty              REAL NOT NULL,
    filled_qty       REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    status           TEXT NOT NULL DEFAULT 'new',
    meta_stop_price       REAL,   -- purpose='entry' only: stop price to attach once filled
    meta_risk_per_share   REAL    -- purpose='entry' only: 1R in $/share, for stage math
);

CREATE TABLE IF NOT EXISTS closed_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    qty           INTEGER NOT NULL,
    entry_time    TEXT NOT NULL,
    exit_time     TEXT NOT NULL,
    r_multiple    REAL NOT NULL,
    exit_reason   TEXT NOT NULL,
    realized_pnl  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_time   TEXT NOT NULL UNIQUE,
    started_at   TEXT NOT NULL,
    completed_at TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    notes        TEXT
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str = "bot.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str = "bot.db") -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- positions ---

def upsert_position(conn: sqlite3.Connection, position: dict) -> None:
    position = dict(position)
    position["last_updated"] = _now_iso()
    columns = [
        "symbol", "entry_order_id", "qty", "initial_qty", "entry_price",
        "entry_time", "initial_stop", "current_stop", "current_stop_order_id",
        "risk_per_share", "partial_target_price", "breakeven_price", "stage",
        "last_updated",
    ]
    values = [position.get(c) for c in columns]
    placeholders = ",".join("?" for _ in columns)
    update_clause = ",".join(f"{c}=excluded.{c}" for c in columns if c != "symbol")
    conn.execute(
        f"""INSERT INTO positions ({",".join(columns)}) VALUES ({placeholders})
            ON CONFLICT(symbol) DO UPDATE SET {update_clause}""",
        values,
    )
    conn.commit()


def get_position(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM positions WHERE symbol = ?", (symbol,)
    ).fetchone()


def get_open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM positions").fetchall()


def delete_position(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
    conn.commit()


# --- closed trades ---

def record_closed_trade(conn: sqlite3.Connection, trade: dict) -> None:
    conn.execute(
        """INSERT INTO closed_trades
           (symbol, entry_price, exit_price, qty, entry_time, exit_time,
            r_multiple, exit_reason, realized_pnl)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            trade["symbol"], trade["entry_price"], trade["exit_price"],
            trade["qty"], trade["entry_time"], trade["exit_time"],
            trade["r_multiple"], trade["exit_reason"], trade["realized_pnl"],
        ),
    )
    conn.commit()


def get_closed_trades(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM closed_trades ORDER BY exit_time"
    ).fetchall()


# --- pending orders ---

def upsert_pending_order(conn: sqlite3.Connection, order: dict) -> None:
    columns = [
        "order_id", "symbol", "purpose", "submitted_at", "qty",
        "filled_qty", "filled_avg_price", "status",
        "meta_stop_price", "meta_risk_per_share",
    ]
    order = dict(order)
    order.setdefault("submitted_at", _now_iso())
    order.setdefault("filled_qty", 0)
    order.setdefault("filled_avg_price", None)
    order.setdefault("status", "new")
    order.setdefault("meta_stop_price", None)
    order.setdefault("meta_risk_per_share", None)
    values = [order.get(c) for c in columns]
    placeholders = ",".join("?" for _ in columns)
    update_clause = ",".join(f"{c}=excluded.{c}" for c in columns if c != "order_id")
    conn.execute(
        f"""INSERT INTO pending_orders ({",".join(columns)}) VALUES ({placeholders})
            ON CONFLICT(order_id) DO UPDATE SET {update_clause}""",
        values,
    )
    conn.commit()


def get_pending_orders(conn: sqlite3.Connection, purpose: str | None = None) -> list[sqlite3.Row]:
    if purpose is None:
        return conn.execute("SELECT * FROM pending_orders").fetchall()
    return conn.execute(
        "SELECT * FROM pending_orders WHERE purpose = ?", (purpose,)
    ).fetchall()


def get_pending_orders_for_symbol(
    conn: sqlite3.Connection, symbol: str, purpose: str | None = None
) -> list[sqlite3.Row]:
    if purpose is None:
        return conn.execute(
            "SELECT * FROM pending_orders WHERE symbol = ?", (symbol,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM pending_orders WHERE symbol = ? AND purpose = ?",
        (symbol, purpose),
    ).fetchall()


def remove_pending_order(conn: sqlite3.Connection, order_id: str) -> None:
    conn.execute("DELETE FROM pending_orders WHERE order_id = ?", (order_id,))
    conn.commit()


# --- cycle log ---

def start_cycle(conn: sqlite3.Connection, cycle_time: str) -> int:
    conn.execute(
        """INSERT INTO cycle_log (cycle_time, started_at, status)
           VALUES (?, ?, 'running')
           ON CONFLICT(cycle_time) DO UPDATE SET started_at=excluded.started_at, status='running'""",
        (cycle_time, _now_iso()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM cycle_log WHERE cycle_time = ?", (cycle_time,)
    ).fetchone()
    return row["id"]


def complete_cycle(conn: sqlite3.Connection, cycle_id: int, status: str, notes: str = "") -> None:
    conn.execute(
        "UPDATE cycle_log SET completed_at = ?, status = ?, notes = ? WHERE id = ?",
        (_now_iso(), status, notes, cycle_id),
    )
    conn.commit()


def get_cycle(conn: sqlite3.Connection, cycle_time: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM cycle_log WHERE cycle_time = ?", (cycle_time,)
    ).fetchone()
