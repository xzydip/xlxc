import time
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS vps (
    vmid       INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL,
    hostname   TEXT    NOT NULL,
    ram_mb     INTEGER NOT NULL,
    cpu        INTEGER NOT NULL,
    disk_gb    INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER,                      -- NULL = permanent
    status     TEXT    NOT NULL DEFAULT 'active',  -- active | suspended | deleted
    warned     INTEGER NOT NULL DEFAULT 0,
    root_password TEXT,
    os_image   TEXT,
    ssh_port   INTEGER NOT NULL DEFAULT 22
);
CREATE TABLE IF NOT EXISTS shares (
    vmid    INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (vmid, user_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS mining_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    vmid     INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    score    INTEGER NOT NULL,
    evidence TEXT,
    action   TEXT
);
CREATE TABLE IF NOT EXISTS admins (          -- admins added with /addadmin (env ADMIN_IDS = owners)
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER NOT NULL,
    added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS free_deploys (    -- VPS given through /deploy
    vmid    INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    ts      INTEGER NOT NULL
);
"""

UPDATABLE = {"owner_id", "hostname", "ram_mb", "cpu", "disk_gb",
             "expires_at", "status", "warned", "root_password", "os_image", "ssh_port"}


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # Migrations for databases created by older versions.
        for stmt in (
            "ALTER TABLE vps ADD COLUMN root_password TEXT",
            "ALTER TABLE vps ADD COLUMN os_image TEXT",
            "ALTER TABLE vps ADD COLUMN ssh_port INTEGER NOT NULL DEFAULT 22",
        ):
            try:
                await self.conn.execute(stmt)
            except Exception:
                pass
        await self.conn.commit()

    async def _all(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchall()

    async def _one(self, sql, args=()):
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    # ---------- VPS ----------
    async def add_vps(self, vmid, owner_id, hostname, ram_mb, cpu, disk_gb, expires_at,
                      root_password=None, os_image=None, ssh_port=22):
        await self.conn.execute(
            "INSERT INTO vps (vmid, owner_id, hostname, ram_mb, cpu, disk_gb, created_at, expires_at, root_password, os_image, ssh_port) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vmid, owner_id, hostname, ram_mb, cpu, disk_gb, int(time.time()), expires_at,
             root_password, os_image, ssh_port),
        )
        await self.conn.commit()

    async def vmid_used(self, vmid: int) -> bool:
        """True if this VMID was EVER used (even deleted) — vmid is the PRIMARY KEY, never reuse."""
        return await self._one("SELECT 1 FROM vps WHERE vmid=?", (vmid,)) is not None

    async def get_vps(self, vmid: int):
        return await self._one("SELECT * FROM vps WHERE vmid=? AND status!='deleted'", (vmid,))

    async def update_vps(self, vmid: int, **fields):
        fields = {k: v for k, v in fields.items() if k in UPDATABLE}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE vps SET {sets} WHERE vmid=?", (*fields.values(), vmid))
        await self.conn.commit()

    async def list_vps(self, owner_id: int | None = None):
        if owner_id is None:
            return await self._all("SELECT * FROM vps WHERE status!='deleted' ORDER BY vmid")
        return await self._all(
            "SELECT * FROM vps WHERE status!='deleted' AND owner_id=? ORDER BY vmid", (owner_id,))

    async def accessible_vps(self, user_id: int):
        return await self._all(
            "SELECT * FROM vps WHERE status!='deleted' AND (owner_id=? OR vmid IN "
            "(SELECT vmid FROM shares WHERE user_id=?)) ORDER BY vmid", (user_id, user_id))

    async def expirable(self):
        """All non-deleted VPS that have an expiry date (permanent ones are skipped)."""
        return await self._all(
            "SELECT * FROM vps WHERE status!='deleted' AND expires_at IS NOT NULL")

    # ---------- shares ----------
    async def add_share(self, vmid, user_id):
        await self.conn.execute("INSERT OR IGNORE INTO shares VALUES (?,?)", (vmid, user_id))
        await self.conn.commit()

    async def remove_share(self, vmid, user_id) -> bool:
        cur = await self.conn.execute("DELETE FROM shares WHERE vmid=? AND user_id=?", (vmid, user_id))
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_shares(self, vmid) -> list[int]:
        rows = await self._all("SELECT user_id FROM shares WHERE vmid=?", (vmid,))
        return [r["user_id"] for r in rows]

    async def clear_shares(self, vmid):
        await self.conn.execute("DELETE FROM shares WHERE vmid=?", (vmid,))
        await self.conn.commit()

    # ---------- settings ----------
    async def get_setting(self, key: str, default=None):
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    # ---------- mining events ----------
    async def add_mining_event(self, vmid, owner_id, score, evidence, action):
        await self.conn.execute(
            "INSERT INTO mining_events (vmid, owner_id, ts, score, evidence, action) VALUES (?,?,?,?,?,?)",
            (vmid, owner_id, int(time.time()), score, evidence, action))
        await self.conn.commit()

    # ---------- admins ----------
    async def admin_ids(self) -> set[int]:
        return {r["user_id"] for r in await self._all("SELECT user_id FROM admins")}

    async def add_admin(self, user_id: int, added_by: int):
        await self.conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?,?,?)",
            (user_id, added_by, int(time.time())))
        await self.conn.commit()

    async def remove_admin(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------- free deploys ----------
    async def add_free(self, user_id: int, vmid: int):
        await self.conn.execute("INSERT INTO free_deploys (vmid, user_id, ts) VALUES (?,?,?)",
                                (vmid, user_id, int(time.time())))
        await self.conn.commit()

    async def user_free_vps(self, user_id: int):
        """Backward-compatible helper: return one active free VPS for a user."""
        return await self._one(
            "SELECT v.* FROM free_deploys f JOIN vps v ON v.vmid=f.vmid "
            "WHERE f.user_id=? AND v.status!='deleted' LIMIT 1", (user_id,))

    async def count_user_free_active(self, user_id: int) -> int:
        """Count active free/deploy VPS owned by this user."""
        row = await self._one(
            "SELECT COUNT(*) AS n FROM free_deploys f JOIN vps v ON v.vmid=f.vmid "
            "WHERE f.user_id=? AND v.status!='deleted'", (user_id,))
        return int(row["n"] or 0)

    async def count_free_active(self) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS n FROM free_deploys f JOIN vps v ON v.vmid=f.vmid "
            "WHERE v.status!='deleted'")
        return row["n"]

    async def mining_banned(self, user_id: int) -> bool:
        """User had a VPS stopped/deleted for mining -> no more free VPS."""
        return await self._one(
            "SELECT 1 FROM mining_events WHERE owner_id=? AND action IN ('deleted','stopped') LIMIT 1",
            (user_id,)) is not None
