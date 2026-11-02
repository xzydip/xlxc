import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import cfg
from database import Database
from proxmox_client import PVE
from utils import refresh_admins

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pvebot")

EXTENSIONS = ("cogs.admins", "cogs.admin", "cogs.deploy", "cogs.manage", "cogs.share",
              "cogs.expiry", "cogs.antimining", "cogs.load", "cogs.autoscan")


class VPSBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
        self.db = Database(cfg.db_path)
        self.pve = PVE()
        self.create_lock = asyncio.Lock()   # one VPS creation at a time (VMID race-free)
        self.tree.on_error = self.on_tree_error
        self.update_vps_presence.start()

    async def setup_hook(self):
        await self.db.connect()
        await refresh_admins(self.db)
        try:   # verify the LXD host is reachable
            await self.pve._sh("command -v lxc >/dev/null && lxc list >/dev/null", timeout=30)
            log.info("LXD host check OK (lxc works)")
        except Exception as e:
            log.error("⚠️ `lxc` host par nahi chal raha (%s). LXD host par bot chalao, "
                      "ya .env me HOST_EXEC_MODE=ssh + HOST_SSH_* do. VPS commands tab tak fail honge.", e)
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        if cfg.guild_id:
            guild = discord.Object(cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.update_vps_presence()

    @tasks.loop(seconds=5)
    async def update_vps_presence(self):
        """Show the number of currently RUNNING LXD VPS instances in Discord status."""
        try:
            items = await self.pve._list_raw()
            running = sum(1 for item in items.values() if item.get("state") == "running")
            await self.change_presence(
                status=discord.Status.online,
                activity=discord.Game(name=f"{running} VPS Instances Running"),
            )
        except Exception as e:
            log.warning("Presence update failed: %s", e)

    @update_vps_presence.before_loop
    async def before_update_vps_presence(self):
        await self.wait_until_ready()

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = str(error) or "❌ Permission nahi hai."
        else:
            log.exception("Command error", exc_info=error)
            msg = f"❌ Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    VPSBot().run(cfg.token)
