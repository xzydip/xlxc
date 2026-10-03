import discord
from discord import app_commands
from discord.ext import commands

from utils import ADMINS, admin_only, is_owner, owner_only, refresh_admins
from config import cfg


class Admins(commands.Cog):
    """/addadmin, /removeadmin, /admins

    Owner = OWNER_ID in .env (cannot be removed from Discord).
    Admins added here are stored in the DB and get every admin command, but cannot add/remove admins.
    """

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await refresh_admins(self.bot.db)

    @app_commands.command(name="addadmin", description="Kisi user ko bot admin banao (sirf owner)")
    @app_commands.describe(user="Jise admin banana hai")
    @owner_only()
    async def addadmin(self, interaction: discord.Interaction, user: discord.User):
        if user.bot:
            return await interaction.response.send_message("❌ Bot ko admin nahi bana sakte.", ephemeral=True)
        if user.id in ADMINS:
            return await interaction.response.send_message(f"ℹ️ {user.mention} pehle se admin hai.",
                                                           ephemeral=True)
        await self.bot.db.add_admin(user.id, interaction.user.id)
        await refresh_admins(self.bot.db)
        await interaction.response.send_message(f"✅ {user.mention} ab bot admin hai.", ephemeral=True)

    @app_commands.command(name="removeadmin", description="Bot admin hatao (sirf owner)")
    @app_commands.describe(user="Jis admin ko hatana hai")
    @owner_only()
    async def removeadmin(self, interaction: discord.Interaction, user: discord.User):
        if is_owner(user.id):
            return await interaction.response.send_message(
                "❌ Owner ko hata nahi sakte — wo `.env` ke `OWNER_ID` me hai.", ephemeral=True)
        ok = await self.bot.db.remove_admin(user.id)
        await refresh_admins(self.bot.db)
        msg = f"✅ {user.mention} ka admin access hata diya." if ok else "ℹ️ Ye user admin nahi tha."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="admins", description="Saare bot admins ki list (admin)")
    @admin_only()
    async def admins(self, interaction: discord.Interaction):
        owners = f"<@{cfg.owner_id}>" if cfg.owner_id else "—"
        extra = sorted(ADMINS - ({cfg.owner_id} if cfg.owner_id else set()))
        added = " ".join(f"<@{i}>" for i in extra) or "—"
        e = discord.Embed(title="🛡️ Bot Admins", color=discord.Color.blurple())
        e.add_field(name="👑 Owner (.env)", value=owners, inline=False)
        e.add_field(name="➕ Added admins", value=added, inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admins(bot))
