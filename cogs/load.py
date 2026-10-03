import discord
from discord import app_commands
from discord.ext import commands

from proxmox_client import PVEError
from utils import admin_only

MAX_LINES = 20   # embed description limit is 4096 chars


def _bar(pct: float) -> str:
    return "🔴" if pct >= 95 else "🟠" if pct >= 90 else "🟡"


async def build_embed(bot, threshold: int, mode: str) -> discord.Embed:
    live = await bot.pve.list_live()          # one API call for every container
    rows = await bot.db.list_vps()

    running, hits = 0, []
    for r in rows:
        l = live.get(r["vmid"])
        if not l or l["state"] != "running" or not l["maxmem"]:
            continue
        running += 1
        cpu = l["cpu"] * 100
        ram = l["mem"] / l["maxmem"] * 100
        both = cpu >= threshold and ram >= threshold
        either = cpu >= threshold or ram >= threshold
        if (mode == "both" and both) or (mode == "any" and either):
            hits.append((r, l, cpu, ram))

    hits.sort(key=lambda h: h[2] + h[3], reverse=True)   # heaviest first
    rule = "CPU **aur** RAM dono" if mode == "both" else "CPU **ya** RAM"

    if not hits:
        return discord.Embed(
            title="📊 VPS Load",
            description=f"✅ Koi VPS nahi mila jiska {rule} ≥ **{threshold}%** ho.\n"
                        f"Running VPS checked: **{running}**",
            color=discord.Color.green(), timestamp=discord.utils.utcnow())

    lines = []
    for r, l, cpu, ram in hits[:MAX_LINES]:
        icon = _bar(max(cpu, ram))
        lines.append(
            f"{icon} `{r['vmid']}` **{r['hostname']}** — <@{r['owner_id']}>\n"
            f"　CPU **{cpu:.0f}%** • RAM **{ram:.0f}%** "
            f"({l['mem'] // 2**20}/{l['maxmem'] // 2**20} MB)")
    if len(hits) > MAX_LINES:
        lines.append(f"…aur **{len(hits) - MAX_LINES}** VPS.")

    e = discord.Embed(
        title=f"📊 High Load VPS ({len(hits)})",
        description="\n".join(lines),
        color=discord.Color.red(), timestamp=discord.utils.utcnow())
    e.set_footer(text=f"Filter: {rule} ≥ {threshold}% • Running checked: {running}")
    return e


class LoadView(discord.ui.View):
    def __init__(self, bot, user_id: int, threshold: int, mode: str):
        super().__init__(timeout=300)
        self.bot, self.user_id = bot, user_id
        self.threshold, self.mode = threshold, mode

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Refresh", emoji="🔃", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        try:
            embed = await build_embed(self.bot, self.threshold, self.mode)
        except PVEError as e:
            return await interaction.followup.send(f"❌ `{e}`", ephemeral=True)
        await interaction.edit_original_response(embed=embed, view=self)


class Load(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="load", description="Jo VPS CPU/RAM ~100% kha rahe hain unki list (admin)")
    @app_commands.describe(
        threshold="Kitne % ya usse upar dikhana hai (default 90)",
        mode="Dono (CPU + RAM) ya koi ek")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Dono — CPU aur RAM", value="both"),
        app_commands.Choice(name="Koi ek — CPU ya RAM", value="any"),
    ])
    @admin_only()
    async def load(self, interaction: discord.Interaction,
                   threshold: app_commands.Range[int, 1, 100] = 90, mode: str = "both"):
        await interaction.response.defer(ephemeral=True)
        try:
            embed = await build_embed(self.bot, threshold, mode)
        except PVEError as e:
            return await interaction.followup.send(f"❌ Proxmox error: `{e}`", ephemeral=True)
        await interaction.followup.send(
            embed=embed, view=LoadView(self.bot, interaction.user.id, threshold, mode), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Load(bot))
