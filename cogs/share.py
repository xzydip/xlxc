import discord
from discord import app_commands
from discord.ext import commands

from utils import dm, is_admin, vps_autocomplete


class Share(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _owner_row(self, interaction, vmid):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
            return None
        if not (is_admin(interaction.user.id) or row["owner_id"] == interaction.user.id):
            await interaction.response.send_message("❌ Sirf owner ya admin share kar sakta hai.", ephemeral=True)
            return None
        return row

    @app_commands.command(name="share", description="VPS kisi aur user ke saath share karo")
    @app_commands.describe(vmid="VPS ID", user="Jis user ko access dena hai")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    async def share(self, interaction: discord.Interaction, vmid: int, user: discord.User):
        row = await self._owner_row(interaction, vmid)
        if not row:
            return
        if user.bot or user.id == row["owner_id"]:
            return await interaction.response.send_message("❌ Is user ko share nahi kar sakte.", ephemeral=True)
        await self.bot.db.add_share(vmid, user.id)
        await dm(self.bot, user.id,
                 f"🤝 <@{interaction.user.id}> ne VPS `{vmid}` tumhare saath share kiya. `/manage` use karo.")
        await interaction.response.send_message(f"✅ VPS `{vmid}` ab {user.mention} ke saath shared hai.", ephemeral=True)

    @app_commands.command(name="unshare", description="VPS ka shared access hatao")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    async def unshare(self, interaction: discord.Interaction, vmid: int, user: discord.User):
        if not await self._owner_row(interaction, vmid):
            return
        ok = await self.bot.db.remove_share(vmid, user.id)
        msg = f"✅ {user.mention} ka access hata diya." if ok else "ℹ️ Ye user shared list me nahi tha."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Share(bot))
