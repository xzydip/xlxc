import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import cfg
from miner_detector import DetectorError, MinerDetector
from proxmox_client import PVEError
from utils import admin_only, dm, send_log, vps_autocomplete

log = logging.getLogger("pvebot.mining")
ALERT_COOLDOWN = 3600   # seconds between "suspicious" alerts for the same VPS


class AntiMining(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.detector = MinerDetector(bot.pve)
        self._last_alert: dict[int, float] = {}
        self.scanner.start()

    def cog_unload(self):
        self.scanner.cancel()

    # ───────────── /setlog ─────────────
    @app_commands.command(name="setlog", description="Mining alerts ka log channel set karo (admin)")
    @app_commands.describe(channel="Jis channel me mining detection logs aayenge")
    @app_commands.guild_only()
    @admin_only()
    async def setlog(self, interaction: discord.Interaction, channel: discord.TextChannel):
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"❌ Mujhe {channel.mention} me **View Channel, Send Messages, Embed Links** permission do.",
                ephemeral=True)
        await self.bot.db.set_setting("log_channel_id", str(channel.id))
        await channel.send(embed=discord.Embed(
            title="🛡️ Mining Protection Active",
            description="Is channel me mining detection logs aayenge.",
            color=discord.Color.green()))
        await interaction.response.send_message(f"✅ Log channel set: {channel.mention}", ephemeral=True)

    # ───────────── /scan (dry run, no action) ─────────────
    @app_commands.command(name="scan", description="VPS ko mining ke liye scan karo — sirf check, delete nahi (admin)")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @admin_only()
    async def scan(self, interaction: discord.Interaction, vmid: int):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            live = await self.bot.pve.status(vmid)
            if live["state"] != "running":
                return await interaction.followup.send("ℹ️ VPS running nahi hai.", ephemeral=True)
            f = await self.detector.scan(vmid, live, record_cpu=False)
        except (PVEError, DetectorError) as e:
            return await interaction.followup.send(f"❌ Scan fail: `{e}`", ephemeral=True)

        if f.score >= cfg.mining_delete_score:
            verdict, color = "🚨 Mining confirmed", discord.Color.red()
        elif f.score >= cfg.mining_alert_score:
            verdict, color = "⚠️ Suspicious", discord.Color.orange()
        else:
            verdict, color = "✅ Clean", discord.Color.green()
        e = discord.Embed(title=f"Scan — VPS {vmid}", description=verdict, color=color)
        e.add_field(name="Score", value=f"{f.score} (delete ≥ {cfg.mining_delete_score})")
        e.add_field(name="Findings", value="\n".join(f"• {r}" for r in f.reasons) or "Kuch nahi mila.",
                    inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    # ───────────── background scanner ─────────────
    @tasks.loop(seconds=cfg.mining_scan_interval)
    async def scanner(self):
        if not cfg.mining_enabled:
            return
        for row in await self.bot.db.list_vps():
            if row["status"] != "active" or row["vmid"] in cfg.mining_exempt:
                continue
            try:
                await self._check(row)
            except (PVEError, DetectorError) as e:
                log.warning("Scan failed for VPS %s: %s", row["vmid"], e)
            except Exception:
                log.exception("Unexpected scan error for VPS %s", row["vmid"])

    @scanner.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    async def _check(self, row):
        vmid = row["vmid"]
        live = await self.bot.pve.status(vmid)
        if live["state"] != "running":
            self.detector.forget(vmid)
            return
        f = await self.detector.scan(vmid, live)
        if f.score >= cfg.mining_delete_score:
            await self._punish(row, f)
        elif f.score >= cfg.mining_alert_score:
            await self._alert(row, f)

    async def _name(self, user_id: int) -> str:
        try:
            return (await self.bot.fetch_user(user_id)).name
        except discord.HTTPException:
            return str(user_id)

    # ───────────── actions ─────────────
    async def _punish(self, row, f):
        vmid, owner_id = row["vmid"], row["owner_id"]
        if cfg.mining_action != "delete":
            return await self._alert(row, f, confirmed=True)

        name = await self._name(owner_id)
        evidence = "\n".join(f.reasons)[:1000]
        deleted, err = True, None
        try:
            await self.bot.pve.stop(vmid, force=True)   # kill the miner right away
            await self.bot.pve.delete(vmid)
        except PVEError as e:
            deleted, err = False, str(e)

        if deleted:
            await self.bot.db.update_vps(vmid, status="deleted")
            await self.bot.db.clear_shares(vmid)
        else:
            await self.bot.db.update_vps(vmid, status="suspended")
        await self.bot.db.add_mining_event(vmid, owner_id, f.score, evidence,
                                           "deleted" if deleted else "stopped")
        self.detector.forget(vmid)

        outcome = "the VPS got deleted" if deleted else "the VPS was stopped (delete failed — delete manually)"
        embed = discord.Embed(title="⚠️ Mining Detected",
                              description=f"⚠️ **{name}** is mining on VPS `{vmid}` and {outcome}.",
                              color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}> (`{owner_id}`)")
        embed.add_field(name="🖥️ VPS", value=f"`{vmid}` — {row['hostname']}")
        embed.add_field(name="🎯 Score", value=str(f.score))
        embed.add_field(name="🔎 Evidence", value=evidence or "—", inline=False)
        if err:
            embed.add_field(name="❌ Delete error", value=f"`{err}`"[:500], inline=False)
        await send_log(self.bot, embed=embed)
        await dm(self.bot, owner_id,
                 f"⛔ Tumhara VPS `{vmid}` **mining detect hone par** "
                 f"{'delete' if deleted else 'stop'} kar diya gaya. Mining allowed nahi hai.")
        log.warning("Mining on VPS %s (owner %s) -> %s", vmid, owner_id, "deleted" if deleted else "stopped")

    async def _alert(self, row, f, confirmed: bool = False):
        vmid = row["vmid"]
        if time.time() - self._last_alert.get(vmid, 0) < ALERT_COOLDOWN:
            return
        self._last_alert[vmid] = time.time()
        name = await self._name(row["owner_id"])
        if confirmed:
            text = (f"🚨 **{name}** is mining on VPS `{vmid}` "
                    f"(auto-delete OFF — `MINING_ACTION=alert`).")
        else:
            text = f"🔎 Suspicious activity on VPS `{vmid}` (owner **{name}**) — possible mining."
        embed = discord.Embed(title="⚠️ Mining Alert", description=text,
                              color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 Owner", value=f"<@{row['owner_id']}>")
        embed.add_field(name="🎯 Score", value=str(f.score))
        embed.add_field(name="🔎 Evidence", value="\n".join(f.reasons)[:1000] or "—", inline=False)
        await send_log(self.bot, embed=embed)
        await self.bot.db.add_mining_event(vmid, row["owner_id"], f.score,
                                           "\n".join(f.reasons)[:1000], "alert")


async def setup(bot):
    await bot.add_cog(AntiMining(bot))
