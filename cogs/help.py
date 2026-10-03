import discord
from discord import app_commands
from discord.ext import commands


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="help", description="Bot ke available commands dekho")
    async def help_cmd(self, interaction: discord.Interaction):
        e = discord.Embed(
            title="📚 X LXC Bot — Help",
            description="Neeche available commands hain. Kuch commands sirf admins ke liye hain.",
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="🖥️ VPS",
            value=(
                "`/deploy` — Free VPS deploy karo\n"
                "`/manage` — VPS select karke management panel kholo\n"
                "`/info` — VPS credentials/status dekho\n"
                "`/rebuild` — VPS ko selected OS se rebuild karo"
            ),
            inline=False,
        )
        e.add_field(
            name="🔐 Manage Panel",
            value="`Start` • `Stop` • `Restart` • `SSH` • `SSHX` • `Private IP` • `Network & Ports` • `Password` • `Rebuild` • `Refresh`",
            inline=False,
        )
        e.add_field(
            name="🛠️ Admin",
            value=(
                "`/adminadd` • `/adminremove` • `/adminlist`\n"
                "`/deployspecs` • `/deployvps`\n"
                "Admin-only commands ke options Discord me automatically show honge."
            ),
            inline=False,
        )
        e.set_footer(text="X LXC Bot")
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Help(bot))
