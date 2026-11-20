"""SQLite 访问层：网页进程写，权威/解析器进程读（同一文件，WAL 模式）。

表：
- records           现行 A 记录（网页写，权威读）
- settings          全局/每域名设置（负 TTL、回包延迟、丢包开关）
- retired_addresses 退役地址保留表：ip -> 保留截止时刻 hold_until。
                    保留期内禁止把该地址写入任何 A 记录；解析器也不得再发出
                    (name, ip) 旧绑定。重启不丢（在共享卷上），调低 TTL 不影响。
- answer_log        解析器答复日志：每发出一个含 A 记录的答复写一行
                    (name, ip, sent_at, ttl)。退役时按 max(sent_at + ttl)
                    计算“客户端手里的旧答复全部到期”的时刻。
"""
import os
import sqlite3
import time
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    name       TEXT PRIMARY KEY,
    ip         TEXT NOT NULL,
    ttl        INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retired_addresses (
    ip         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,   -- 退役时该地址所属的域名
    retired_at REAL NOT NULL,   -- 退役操作时刻（epoch 秒）
    hold_until REAL NOT NULL    -- 保留截止时刻（epoch 秒），此前禁止再分配
);

CREATE TABLE IF NOT EXISTS answer_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    ip      TEXT NOT NULL,
    sent_at REAL NOT NULL,      -- 解析器发包时刻（epoch 秒）
    ttl     INTEGER NOT NULL    -- 报文中的 TTL（客户端据此缓存）
);
CREATE INDEX IF NOT EXISTS idx_answer_log_ip ON answer_log(ip);
"""


class NoRecord(Exception):
    """退役时该域名没有现行 A 记录。"""


class RetireConflict(Exception):
    """另一条现行 A 记录仍使用该地址，拒绝退役。"""

    def __init__(self, other_name: str):
        super().__init__(other_name)
        self.other_name = other_name


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    """每次调用返回新连接。

    权威端每个查询开一个短连接，保证网页写入后立刻能读到最新数据；
    WAL 模式允许 web 写与 authoritative 读并发而互不阻塞。
    """
    path = db_path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db() -> None:
    """建表并写入默认设置 / 种子记录（仅 web 启动时调用一次）。"""
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        defaults = {
            "neg_ttl": str(config.DEFAULT_NEG_TTL),
        }
        for name in config.DOMAINS:
            defaults[f"delay_{name}"] = str(config.DEFAULT_DELAY_MS)
            defaults[f"dropped_{name}"] = "0"
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        # 第一个域名放种子 A 记录，第二个默认不存在（便于演示 NXDOMAIN 负缓存）。
        # 种子只在首次初始化播种一次（seeded 标志），且库里已有退役条目时绝不播种：
        # 否则 web 重启会把已撤销的记录重新插回来，破坏“重启不得提前放行 /
        # 不得复活旧绑定”的语义。
        cur = conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('seeded', '1')")
        first_boot = cur.rowcount == 1
        retired_n = conn.execute("SELECT COUNT(*) AS n FROM retired_addresses").fetchone()["n"]
        if config.DOMAINS and first_boot and retired_n == 0:
            conn.execute(
                "INSERT OR IGNORE INTO records(name, ip, ttl, updated_at) VALUES (?, ?, ?, ?)",
                (config.DOMAINS[0], "10.0.0.10", config.DEFAULT_POS_TTL, time.time()),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------- records ----------------

def get_record(conn: sqlite3.Connection, name: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM records WHERE name = ?", (name,)).fetchone()


def list_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM records ORDER BY name").fetchall()


def upsert_record(name: str, ip: str, ttl: int) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO records(name, ip, ttl, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                ip = excluded.ip,
                ttl = excluded.ttl,
                updated_at = excluded.updated_at
            """,
            (name, ip, ttl, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_record(name: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM records WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


# ---------------- 退役 / 地址保留 ----------------

def retire_record(name: str, cache_remaining: int = 0, now: Optional[float] = None) -> dict:
    """退役一个域名的 A 记录：删除记录 + 写入地址保留期限，同一事务提交。

    - 另一条现行 A 记录仍用同一地址 -> RetireConflict，整体回滚；
    - 该域名没有现行记录 -> NoRecord，整体回滚；
    - 保留截止 hold_until = max(答复日志中该地址最晚的 sent_at+ttl,
                                当前解析器缓存剩余 TTL 对应的到期时刻, now)。
      即“解析器先前发出的旧答复全部到期”之前，地址不得再分配。
    cache_remaining 由调用方（web）事先从解析器状态口读取并传入。
    """
    now = time.time() if now is None else now
    conn = connect()
    try:
        # BEGIN IMMEDIATE：检查冲突 -> 删除 -> 写保留 在同一写事务内串行化
        conn.execute("BEGIN IMMEDIATE")
        row = get_record(conn, name)
        if row is None:
            raise NoRecord(name)
        ip = row["ip"]
        other = conn.execute(
            "SELECT name FROM records WHERE ip = ? AND name != ? LIMIT 1",
            (ip, name),
        ).fetchone()
        if other is not None:
            raise RetireConflict(other["name"])
        # 已到期的答复日志不影响 max，顺手清理，控制表体积
        conn.execute("DELETE FROM answer_log WHERE sent_at + ttl <= ?", (now,))
        m = conn.execute(
            "SELECT MAX(sent_at + ttl) AS m FROM answer_log WHERE ip = ?", (ip,)
        ).fetchone()["m"]
        hold_until = now
        if m is not None:
            hold_until = max(hold_until, float(m))
        if cache_remaining > 0:
            hold_until = max(hold_until, now + int(cache_remaining))
        conn.execute("DELETE FROM records WHERE name = ?", (name,))
        conn.execute(
            """
            INSERT INTO retired_addresses(ip, name, retired_at, hold_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                name = excluded.name,
                retired_at = excluded.retired_at,
                hold_until = MAX(retired_addresses.hold_until, excluded.hold_until)
            """,
            (ip, name, now, hold_until),
        )
        conn.commit()
        return {"ip": ip, "name": name, "retired_at": now, "hold_until": hold_until}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def extend_hold(ip: str, hold_until: float) -> None:
    """收紧保留期（只延长、不缩短）。

    退役事务提交后，web 再读一次解析器缓存：若竞态窗口内又出现了该地址的
    正缓存且到期更晚，则把 hold_until 延长到覆盖它。
    """
    conn = connect()
    try:
        conn.execute(
            "UPDATE retired_addresses SET hold_until = MAX(hold_until, ?) WHERE ip = ?",
            (float(hold_until), ip),
        )
        conn.commit()
    finally:
        conn.close()


def list_retired(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM retired_addresses ORDER BY hold_until DESC"
    ).fetchall()


def hold_until_for_ip(conn: sqlite3.Connection, ip: str) -> Optional[float]:
    row = conn.execute(
        "SELECT hold_until FROM retired_addresses WHERE ip = ?", (ip,)
    ).fetchone()
    return float(row["hold_until"]) if row else None


def active_retired_pairs(now: Optional[float] = None) -> set[tuple[str, str]]:
    """当前应被拦截的 (name, ip) 退役绑定集合。解析器用。

    拦截与保留期（hold_until）无关：只要绑定被退役、且当前没有同名同 IP 的
    现行记录，就不得再发出——否则迟到的旧回包会把已撤销的绑定复活。
    保留期到期、管理员把同一绑定重新写回 records 后，NOT EXISTS 自动解除拦截。
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT r.name, r.ip FROM retired_addresses r
            WHERE NOT EXISTS (
                SELECT 1 FROM records rec WHERE rec.name = r.name AND rec.ip = r.ip
            )
            """
        ).fetchall()
        return {(r["name"], r["ip"]) for r in rows}
    finally:
        conn.close()


# ---------------- 解析器答复日志 ----------------

def log_answers(rows: list[tuple[str, str, float, int]]) -> None:
    """追加答复日志：(name, ip, sent_at, ttl)。由解析器在发包后调用。"""
    if not rows:
        return
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO answer_log(name, ip, sent_at, ttl) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


# ---------------- settings ----------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_int_setting(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(conn, key, str(default)))
    except ValueError:
        return default


def get_bool_setting(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    return get_setting(conn, key, "1" if default else "0") == "1"


def set_setting(key: str, value: str) -> None:
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()
