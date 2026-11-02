import datetime as dt
import re
import secrets
import string
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from config import cfg
from proxmox_client import PVEError

# ───────────────────────── Expiry parsing ─────────────────────────
# Blank / "permanent" / "never" ... => permanent VPS (expires_at = NULL)
PERMANENT_WORDS = {"", "permanent", "perm", "never", "lifetime", "unlimited", "none", "0"}
UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}


class ExpiryError(ValueError):
    pass


@dataclass
class Expiry:
    kind: str                    # "permanent" | "duration" | "date"
    seconds: int = 0
    timestamp: int | None = None


def parse_expiry(text: str | None) -> Expiry:
    """'7d', '12h', '2w', '1mo', '1y', '30' (=days), '2026-12-31', blank/permanent."""
    t = (text or "").strip().lower()
    if t in PERMANENT_WORDS:
        return Expiry("permanent")

    m = re.fullmatch(r"(\d+)\s*(h|d|w|mo|y)?", t)
    if m:
        n = int(m.group(1))
        if n <= 0:
            raise ExpiryError("Expiry 0 se bada hona chahiye (ya blank = permanent).")
        return Expiry("duration", seconds=n * UNITS[m.group(2) or "d"])

    try:
        d = dt.datetime.strptime(t, "%Y-%m-%d")
    except ValueError:
        raise ExpiryError(
            "Expiry format galat hai. Use: `7d`, `12h`, `2w`, `1mo`, `1y`, "
            "`2026-12-31` ya blank/`permanent`.")
    d = d.replace(hour=23, minute=59, second=59, tzinfo=ZoneInfo(cfg.timezone))
    ts = int(d.timestamp())
    if ts <= time.time():
        raise ExpiryError("Ye date past me hai.")
    return Expiry("date", timestamp=ts)


def resolve_expiry(exp: Expiry, base: int | None = None) -> int | None:
    """Return unix ts (or None = permanent). For durations, counts from `base`
    if base is still in the future (used by /renew), otherwise from now."""
    now = int(time.time())
    if exp.kind == "permanent":
        return None
    if exp.kind == "duration":
        start = base if (base and base > now) else now
        return start + exp.seconds
    return exp.timestamp


def fmt_expiry(ts: int | None) -> str:
    return "♾️ Permanent" if ts is None else f"<t:{ts}:F> (<t:{ts}:R>)"


def is_expired(row) -> bool:
    return row["expires_at"] is not None and row["expires_at"] <= time.time()


# ───────────────────────── Shared command types ─────────────────────────
RAM = app_commands.Range[int, 1, 128]  # RAM input is in GB; DB/LXD uses MB

def ram_gb_to_mb(gb: int) -> int:
    return int(gb) * 1024

def ram_mb_to_gb(mb: int) -> int:
    return max(1, int(round(int(mb) / 1024)))
CPU = app_commands.Range[int, 1, 64]
DISK = app_commands.Range[int, 1, 4000]
EXPIRY_HELP = "7d / 12h / 2w / 1mo / 1y / 2026-12-31 — blank = permanent"


# ───────────────────────── Misc helpers ─────────────────────────
def gen_password(n: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def safe_hostname(name: str | None, fallback: str) -> str:
    name = re.sub(r"[^a-z0-9-]", "", (name or "").lower()).strip("-")
    return name[:40] or fallback


def fmt_uptime(sec: int) -> str:
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    return f"{d}d {h}h {r // 60}m"


# ───────────────────────── Admins ─────────────────────────
# Main owner = OWNER_ID in .env. Extra admins = added at runtime with /addadmin.
# Both live in this set so is_admin() stays a fast, sync check.
ADMINS: set[int] = ({cfg.owner_id} if cfg.owner_id else set())


async def refresh_admins(db) -> None:
    extra = await db.admin_ids()          # fetch first: no window where admins are missing
    ADMINS.clear()
    if cfg.owner_id:
        ADMINS.add(cfg.owner_id)
    ADMINS.update(extra)


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def is_owner(user_id: int) -> bool:
    return bool(cfg.owner_id) and user_id == cfg.owner_id


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user.id):
            return True
        raise app_commands.CheckFailure("❌ Ye command sirf admins ke liye hai.")
    return app_commands.check(predicate)


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_owner(interaction.user.id):
            return True
        raise app_commands.CheckFailure("❌ Ye command sirf bot owner (`OWNER_ID`) ke liye hai.")
    return app_commands.check(predicate)


async def get_accessible(bot, user_id: int, vmid: int):
    """Row if user is admin/owner/shared-user of vmid, else None."""
    row = await bot.db.get_vps(vmid)
    if not row:
        return None
    if is_admin(user_id) or row["owner_id"] == user_id:
        return row
    if user_id in await bot.db.get_shares(vmid):
        return row
    return None


async def dm(bot, user_id: int, content: str | None = None, embed: discord.Embed | None = None) -> bool:
    try:
        user = await bot.fetch_user(user_id)
        await user.send(content=content, embed=embed)
        return True
    except discord.HTTPException:
        return False


async def send_log(bot, content: str | None = None, embed: discord.Embed | None = None) -> bool:
    """Post to the channel configured with /setlog. Falls back to DMing admins."""
    cid = await bot.db.get_setting("log_channel_id")
    if cid:
        try:
            ch = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
            await ch.send(content=content, embed=embed)
            return True
        except (discord.HTTPException, AttributeError):
            pass
    for admin_id in ADMINS:
        await dm(bot, admin_id, content, embed)
    return False


async def reactivate(bot, vmid: int) -> str | None:
    """Mark VPS active again and start it. Returns error text or None."""
    await bot.db.update_vps(vmid, status="active", warned=0)
    try:
        await bot.pve.start(vmid)
    except PVEError as e:
        return str(e)
    return None


async def provision_vps(bot, owner_id: int, ram: int, cpu: int, disk: int,
                        expires_at: int | None, hostname: str | None = None,
                        prefix: str = "vps", image: str | None = None):
    """Create the LXC + DB row. Returns (row, root_password). Raises PVEError.
    Caller MUST hold `bot.create_lock` (keeps VMID picking race-free)."""
    password = gen_password()
    vmid = await bot.pve.next_vmid(cfg.vmid_start)
    while await bot.db.vmid_used(vmid):          # vmid is the DB primary key: never reuse, even deleted
        vmid = await bot.pve.next_vmid(vmid + 1)
    host = safe_hostname(hostname, f"{prefix}-{vmid}")
    chosen_image = image or cfg.image
    await bot.pve.create(vmid, host, cpu, ram, disk, password, image=chosen_image)
    await bot.db.add_vps(vmid, owner_id, host, ram, cpu, disk, expires_at,
                         root_password=password, os_image=chosen_image, ssh_port=22)
    return await bot.db.get_vps(vmid), password


@dataclass
class DeployConf:
    enabled: bool
    ram: int
    cpu: int
    disk: int
    expiry: str


async def get_deploy_conf(db) -> DeployConf:
    """Free-VPS settings: DB (set via /deployspecs, /deployvps) overrides .env defaults."""
    en = await db.get_setting("deploy_enabled")
    saved_limit = await db.get_setting("deploy_user_limit")
    if saved_limit is not None:
        try:
            cfg.free_per_user_limit = max(0, int(saved_limit))
        except ValueError:
            pass
    return DeployConf(
        enabled=cfg.deploy_default_enabled if en is None else en == "1",
        ram=int(await db.get_setting("deploy_ram") or cfg.free_ram_mb),
        cpu=int(await db.get_setting("deploy_cpu") or cfg.free_cpu),
        disk=int(await db.get_setting("deploy_disk") or cfg.free_disk_gb),
        expiry=(await db.get_setting("deploy_expiry")) or cfg.free_expiry,
    )


async def vps_autocomplete(interaction: discord.Interaction, current: str):
    bot = interaction.client
    if is_admin(interaction.user.id):
        rows = await bot.db.list_vps()
    else:
        rows = await bot.db.accessible_vps(interaction.user.id)
    out = []
    for r in rows:
        label = f"{r['vmid']} — {r['hostname']}"
        if current.lower() in label.lower():
            out.append(app_commands.Choice(name=label[:100], value=r["vmid"]))
    return out[:25]


# ───────────────────────── Embeds / views ─────────────────────────
def vps_embed(row, live: dict | None = None, shared: list[int] | None = None) -> discord.Embed:
    colors = {"active": discord.Color.green(), "suspended": discord.Color.red()}
    e = discord.Embed(title=f"🖥️ VPS #{row['vmid']} — {row['hostname']}",
                      color=colors.get(row["status"], discord.Color.blurple()))
    e.add_field(name="👤 Owner", value=f"<@{row['owner_id']}>")
    e.add_field(name="⚙️ Specs",
                value=f"{row['cpu']} vCPU • {ram_mb_to_gb(row['ram_mb'])} GB RAM • {row['disk_gb']} GB")
    e.add_field(name="📅 Expiry", value=fmt_expiry(row["expires_at"]), inline=False)
    status = row["status"].title()
    if row["status"] == "suspended":
        status += " (expired — renew karo)"
    e.add_field(name="📌 Panel Status", value=status)
    if live:
        icon = "🟢" if live["state"] == "running" else "🔴"
        e.add_field(name="⚡ State", value=f"{icon} {live['state']}")
        if live["state"] == "running":
            e.add_field(name="🌐 IP", value=live["ip"] or "DHCP pending…")
            e.add_field(name="🧠 RAM", value=f"{live['mem'] / 2**30:.1f} / {live['maxmem'] / 2**30:.1f} GB")
            e.add_field(name="📈 CPU", value=f"{live['cpu'] * 100:.1f}%")
            e.add_field(name="⏱️ Uptime", value=fmt_uptime(live["uptime"]))
    if shared:
        e.add_field(name="🤝 Shared with", value=" ".join(f"<@{u}>" for u in shared), inline=False)
    return e


class ConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _):
        self.value = False
        await interaction.response.defer()
        self.stop()
