import asyncio
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import cfg
from proxmox_client import PVEError
from utils import (CPU, DISK, EXPIRY_HELP, RAM, ExpiryError, admin_only, dm, fmt_expiry,
                   get_deploy_conf, is_admin, parse_expiry, provision_vps, resolve_expiry,
                   ram_gb_to_mb, ram_mb_to_gb, vps_embed)

log = logging.getLogger("pvebot.deploy")


OS_OPTIONS = [
    ("Ubuntu 22.04", "ubuntu:22.04"),
    ("Ubuntu 24.04", "ubuntu:24.04"),
    ("Debian 12", "images:debian/12"),
]

class OSSelectView(discord.ui.View):
    def __init__(self, bot, owner_id: int, ram: int, cpu: int, disk: int, expires_at: int | None):
        super().__init__(timeout=120)
        self.bot, self.owner_id = bot, owner_id
        self.ram, self.cpu, self.disk, self.expires_at = ram, cpu, disk, expires_at
        select = discord.ui.Select(
            placeholder="OS select karo…",
            options=[discord.SelectOption(label=n, value=v) for n, v in OS_OPTIONS]
        )
        select.callback = self.selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Ye OS selector tumhare deploy ke liye nahi hai.", ephemeral=True)
            return False
        return True

    async def selected(self, interaction: discord.Interaction):
        image = interaction.data.get("values", [None])[0]
        label = next((n for n, v in OS_OPTIONS if v == image), image)
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await interaction.followup.send(
            "🛠️ **Creating VPS**\n\n⏳ Preparing container…", ephemeral=True, wait=True)
        async def animate():
            frames = [
                "🛠️ **Creating VPS**\n\n⏳ Preparing container…",
                "🛠️ **Creating VPS**\n\n⏳ Downloading OS image…",
                "🛠️ **Creating VPS**\n\n⏳ Creating LXC container…",
                "🛠️ **Creating VPS**\n\n⏳ Configuring CPU / RAM / Disk…",
                "🛠️ **Creating VPS**\n\n⏳ Installing SSH…",
                "🛠️ **Creating VPS**\n\n⏳ Finalizing VPS…",
            ]
            i = 0
            try:
                while True:
                    await asyncio.sleep(1.5)
                    i = (i + 1) % len(frames)
                    await msg.edit(content=frames[i])
            except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
                return

        anim = asyncio.create_task(animate())
        try:
            async with self.bot.create_lock:
                row, password = await provision_vps(self.bot, self.owner_id, self.ram, self.cpu, self.disk,
                                                    self.expires_at, prefix="free", image=image)
                await self.bot.db.add_free(self.owner_id, row["vmid"])
        except PVEError as e:
            anim.cancel()
            await asyncio.gather(anim, return_exceptions=True)
            return await msg.edit(content=f"❌ VPS ban nahi paya: `{e}`")
        finally:
            if not anim.done():
                anim.cancel()
            await asyncio.gather(anim, return_exceptions=True)
        embed = vps_embed(row)
        embed.add_field(name="🖥️ OS", value=label)
        embed.add_field(name="🔑 Login", value=f"`root` / `{password}`", inline=False)
        dm_ok = await dm(self.bot, self.owner_id,
                         "✅ Tumhara free VPS ready hai! Password safe rakho. `/manage` se control karo.", embed)
        note = "📩 Credentials DM me bhej diye." if dm_ok else "⚠️ DMs closed hain — credentials abhi copy kar lo."
        await interaction.followup.send(f"✅ Free VPS ready! {note}", embed=embed, ephemeral=True)
        self.stop()


class Deploy(commands.Cog):
    """/deploy (everyone) • /deployspecs (admin) • /deployvps true|false (admin)"""

    def __init__(self, bot):
        self.bot = bot

    # ───────────── /deploy ─────────────
    @app_commands.command(name="deploy", description="Free VPS lo — OS select karke deploy karo")
    async def deploy(self, interaction: discord.Interaction):
        conf = await get_deploy_conf(self.bot.db)
        uid = interaction.user.id
        if not conf.enabled:
            return await interaction.response.send_message("⛔ Free VPS deploy abhi **band** hai.", ephemeral=True)
        if await self.bot.db.mining_banned(uid):
            return await interaction.response.send_message("⛔ Mining detect hone ki wajah se tumhe free VPS nahi mil sakta.", ephemeral=True)
        # /setdeploylimit controls how many active /deploy VPS a normal user may own.
        # 0 = unlimited. Owner/admins bypass this per-user limit.
        if not is_admin(uid) and cfg.free_per_user_limit > 0:
            current = await self.bot.db.count_user_free_active(uid)
            if current >= cfg.free_per_user_limit:
                return await interaction.response.send_message(
                    f"⛔ Deploy limit reached. Tum maximum **{cfg.free_per_user_limit} VPS** deploy kar sakte ho. "
                    f"Current: **{current}**.", ephemeral=True)
        if cfg.free_max_total and await self.bot.db.count_free_active() >= cfg.free_max_total:
            return await interaction.response.send_message("😕 Free VPS ka stock khatam ho gaya. Baad me try karo.", ephemeral=True)
        try:
            expires_at = resolve_expiry(parse_expiry(conf.expiry))
        except ExpiryError as e:
            return await interaction.response.send_message(f"❌ Free VPS expiry setting galat hai: `{e}`", ephemeral=True)
        embed = discord.Embed(title="🚀 Deploy VPS", description=(
            f"Specs: **{conf.cpu} vCPU • {ram_mb_to_gb(conf.ram)} GB RAM • {conf.disk} GB**\n"
            "Neeche apna operating system select karo."))
        await interaction.response.send_message(embed=embed,
            view=OSSelectView(self.bot, uid, conf.ram, conf.cpu, conf.disk, expires_at), ephemeral=True)

    # ───────────── /deployspecs ─────────────
    @app_commands.command(name="deployspecs", description="Free VPS ke specs set karo (admin)")
    @app_commands.describe(ram="RAM in GB (e.g. 8 = 8 GB)", cpu="vCPU cores", disk="Disk in GB",
                           expiry=f"Free VPS expiry: {EXPIRY_HELP}")
    @admin_only()
    async def deployspecs(self, interaction: discord.Interaction,
                          ram: Optional[RAM] = None, cpu: Optional[CPU] = None,
                          disk: Optional[DISK] = None, expiry: Optional[str] = None):
        db = self.bot.db
        if expiry is not None:
            try:
                parse_expiry(expiry)
            except ExpiryError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            await db.set_setting("deploy_expiry", expiry.strip() or "permanent")
        if ram is not None:
            await db.set_setting("deploy_ram", str(ram_gb_to_mb(ram)))
        if cpu is not None:
            await db.set_setting("deploy_cpu", str(cpu))
        if disk is not None:
            await db.set_setting("deploy_disk", str(disk))

        conf = await get_deploy_conf(db)
        changed = any(v is not None for v in (ram, cpu, disk, expiry))
        e = discord.Embed(title="🎁 Free VPS Specs",
                          description="✅ Updated (sirf **naye** deploys par lagega)." if changed
                          else "Current settings (badalne ke liye options do):",
                          color=discord.Color.green() if conf.enabled else discord.Color.red())
        e.add_field(name="⚙️ Specs", value=f"{conf.cpu} vCPU • {ram_mb_to_gb(conf.ram)} GB RAM • {conf.disk} GB")
        e.add_field(name="📅 Expiry", value=conf.expiry)
        e.add_field(name="📌 /deploy", value="🟢 ON" if conf.enabled else "🔴 OFF")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ───────────── /setdeploylimit ─────────────
    @app_commands.command(name="setdeploylimit", description="Har normal user kitne /deploy VPS rakh sakta hai set karo")
    @app_commands.describe(limit="Per-user active /deploy VPS limit; 0 = unlimited")
    @admin_only()
    async def setdeploylimit(self, interaction: discord.Interaction, limit: app_commands.Range[int, 0, 1000]):
        await self.bot.db.set_setting("deploy_user_limit", str(int(limit)))
        cfg.free_per_user_limit = int(limit)
        text = "unlimited" if int(limit) == 0 else str(int(limit))
        await interaction.response.send_message(
            f"✅ `/deploy` per-user limit **{text}** set ho gaya.", ephemeral=True)

    # ───────────── /deployvps ─────────────
    @app_commands.command(name="deployvps", description="Free VPS /deploy on ya off karo (admin)")
    @app_commands.describe(enabled="true = /deploy chalu, false = /deploy band")
    @app_commands.choices(enabled=[app_commands.Choice(name="true — ON", value="true"),
                                   app_commands.Choice(name="false — OFF", value="false")])
    @admin_only()
    async def deployvps(self, interaction: discord.Interaction, enabled: str):
        on = enabled == "true"
        await self.bot.db.set_setting("deploy_enabled", "1" if on else "0")
        await interaction.response.send_message(
            "🟢 Free VPS `/deploy` **ON** ho gaya." if on else "🔴 Free VPS `/deploy` **OFF** ho gaya.",
            ephemeral=True)


async def setup(bot):
    await bot.add_cog(Deploy(bot))
