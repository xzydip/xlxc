"""Background expiry engine.

active   --(now >= expires_at)-->  suspended (container stopped, owner DM'd)
suspended --(grace period over)--> deleted   (container destroyed, owner DM'd)
Permanent VPS (expires_at NULL) are never touched.
/renew or /editvps (new expiry) brings a suspended VPS back (see utils.reactivate).
"""
import logging
import time

from discord.ext import commands, tasks

from config import cfg
from proxmox_client import PVEError
from utils import dm, fmt_expiry

log = logging.getLogger("pvebot.expiry")


class Expiry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.checker.start()

    def cog_unload(self):
        self.checker.cancel()

    @tasks.loop(minutes=cfg.check_interval_min)
    async def checker(self):
        now = int(time.time())
        for r in await self.bot.db.expirable():
            vmid, exp = r["vmid"], r["expires_at"]
            try:
                if r["status"] == "active":
                    if now >= exp:
                        await self._suspend(r)
                    elif exp - now <= cfg.warn_hours * 3600 and not r["warned"]:
                        await self.bot.db.update_vps(vmid, warned=1)
                        await dm(self.bot, r["owner_id"],
                                 f"⏰ VPS `{vmid}` jald expire hoga: {fmt_expiry(exp)}\n"
                                 f"Renew ke liye admin se baat karo.")
                elif r["status"] == "suspended" and now >= exp + cfg.grace_days * 86400:
                    await self._destroy(r)
            except Exception:
                log.exception("Expiry handling failed for VPS %s", vmid)

    async def _suspend(self, r):
        vmid = r["vmid"]
        try:
            await self.bot.pve.stop(vmid, force=True)
        except PVEError as e:
            if "does not exist" not in str(e):
                raise
        await self.bot.db.update_vps(vmid, status="suspended")
        await dm(self.bot, r["owner_id"],
                 f"⛔ VPS `{vmid}` expire ho gaya aur stop kar diya gaya.\n"
                 f"Renew nahi hua to {cfg.grace_days} din baad delete ho jayega.")
        log.info("Suspended VPS %s", vmid)

    async def _destroy(self, r):
        vmid = r["vmid"]
        await self.bot.pve.delete(vmid)      # raises on failure -> stays 'suspended', retried next loop
        await self.bot.db.update_vps(vmid, status="deleted")
        await self.bot.db.clear_shares(vmid)
        await dm(self.bot, r["owner_id"], f"🗑️ VPS `{vmid}` expiry ke baad delete kar diya gaya.")
        log.info("Deleted expired VPS %s", vmid)

    @checker.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Expiry(bot))
