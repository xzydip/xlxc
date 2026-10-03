import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import cfg
from proxmox_client import PVEError, lxc_name
from utils import admin_only, send_log

log = logging.getLogger("pvebot.autoscan")


def _parse(raw: str):
    out = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def usage(bot, vmid: int):
    name = lxc_name(vmid)
    script = r'''set -u
read -r _ u n s i w irq sirq steal _ < /proc/stat
prev_idle=$((i+w)); prev_total=$((u+n+s+i+w+irq+sirq+steal))
sleep 0.5
read -r _ u n s i w irq sirq steal _ < /proc/stat
idle=$((i+w)); total=$((u+n+s+i+w+irq+sirq+steal))
dt=$((total-prev_total)); di=$((idle-prev_idle))
if [ "$dt" -gt 0 ]; then cpu=$(awk -v a="$dt" -v b="$di" 'BEGIN {printf "%.1f", (1-b/a)*100}'); else cpu=0; fi
mem_total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
mem_avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
if [ -z "${mem_total:-}" ]; then mem_total=0; fi
if [ -z "${mem_avail:-}" ]; then mem_avail=0; fi
if [ "$mem_total" -gt 0 ]; then ram=$(awk -v t="$mem_total" -v a="$mem_avail" 'BEGIN {printf "%.1f", (1-a/t)*100}'); else ram=0; fi
printf 'CPU=%s\nRAM=%s\n' "$cpu" "$ram"
'''
    raw = await bot.pve.shell.run(f"lxc exec {name} -- bash -lc {__import__('shlex').quote(script)}", timeout=20)
    vals = _parse(raw)
    return float(vals.get("CPU", 0)), float(vals.get("RAM", 0))


class AutoScan(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_run = 0.0
        self.worker.start()

    def cog_unload(self):
        self.worker.cancel()

    async def _cfg(self):
        enabled = (await self.bot.db.get_setting("autoscan_enabled", "0")) == "1"
        interval = max(1, int(await self.bot.db.get_setting("autoscan_interval", "10")))
        cpu = float(await self.bot.db.get_setting("autoscan_cpu", "90"))
        ram = float(await self.bot.db.get_setting("autoscan_ram", "90"))
        return enabled, interval, cpu, ram

    @app_commands.command(name="autoscan", description="CPU/RAM usage autoscan on/off karo")
    @app_commands.describe(enabled="true ya false", interval="Check interval minutes", cpu="CPU threshold %", ram="RAM threshold %")
    @admin_only()
    async def autoscan(self, interaction: discord.Interaction, enabled: bool, interval: app_commands.Range[int, 1, 1440], cpu: app_commands.Range[int, 1, 100], ram: app_commands.Range[int, 1, 100]):
        await self.bot.db.set_setting("autoscan_enabled", "1" if enabled else "0")
        await self.bot.db.set_setting("autoscan_interval", str(interval))
        await self.bot.db.set_setting("autoscan_cpu", str(cpu))
        await self.bot.db.set_setting("autoscan_ram", str(ram))
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"✅ Autoscan **{state}**\nInterval: `{interval} min`\nCPU: `{cpu}%` • RAM: `{ram}%`", ephemeral=True)

    @app_commands.command(name="autoban", description="CPU/RAM threshold cross hone par VPS suspend karo")
    @app_commands.describe(enabled="true ya false", cpu="CPU threshold %", ram="RAM threshold %")
    @admin_only()
    async def autoban(self, interaction: discord.Interaction, enabled: bool, cpu: app_commands.Range[int, 1, 100], ram: app_commands.Range[int, 1, 100]):
        await self.bot.db.set_setting("autoban_enabled", "1" if enabled else "0")
        await self.bot.db.set_setting("autoban_cpu", str(cpu))
        await self.bot.db.set_setting("autoban_ram", str(ram))
        state = "enabled" if enabled else "disabled"
        await interaction.response.send_message(
            f"✅ Autoban **{state}**\nCPU: `{cpu}%` • RAM: `{ram}%`", ephemeral=True)

    @tasks.loop(seconds=15)
    async def worker(self):
        try:
            enabled, interval, cpu_threshold, ram_threshold = await self._cfg()
            if not enabled or time.monotonic() - self.last_run < interval * 60:
                return
            self.last_run = time.monotonic()
            autoban = (await self.bot.db.get_setting("autoban_enabled", "0")) == "1"
            autoban_cpu = float(await self.bot.db.get_setting("autoban_cpu", str(cpu_threshold)))
            autoban_ram = float(await self.bot.db.get_setting("autoban_ram", str(ram_threshold)))
            for row in await self.bot.db.list_vps():
                if row["status"] != "active":
                    continue
                try:
                    live = await self.bot.pve.status(row["vmid"])
                    if live.get("state") != "running":
                        continue
                    cpu, ram = await usage(self.bot, row["vmid"])
                    cpu_hit, ram_hit = cpu >= cpu_threshold, ram >= ram_threshold
                    if not (cpu_hit or ram_hit):
                        continue
                    overload = "CPU + RAM Overload ⚡" if cpu_hit and ram_hit else ("CPU Overload ⚡" if cpu_hit else "RAM Overload ⚡")
                    if autoban and (cpu >= autoban_cpu or ram >= autoban_ram):
                        ac = cpu >= autoban_cpu
                        ar = ram >= autoban_ram
                        aover = "CPU + RAM Overload ⚡" if ac and ar else ("CPU Overload ⚡" if ac else "RAM Overload ⚡")
                        try:
                            await self.bot.pve.stop(row["vmid"], force=True)
                            await self.bot.db.update_vps(row["vmid"], status="suspended")
                            punishment = "VPS suspend ⚠️"
                        except PVEError as e:
                            punishment = f"Suspend failed: {e}"
                        text = (
                            "**Overload Vm Detected 🚨**\n\n"
                            f"**VPS OWNER**\n<@{row['owner_id']}>\n\n"
                            f"**VMID**\n`{row['vmid']}`\n\n"
                            f"**CPU Usage**\n`{cpu:.1f}%`\n\n"
                            f"**RAM Usage**\n`{ram:.1f}%`\n\n"
                            f"**Threshold**\nCPU `{autoban_cpu:.0f}%` • RAM `{autoban_ram:.0f}%`\n\n"
                            f"**Overload**\n{aover}\n\n"
                            f"**Punishment**\n{punishment}"
                        )
                    else:
                        text = (
                            "**High Usage Detected 🚨**\n\n"
                            f"**VPS OWNER**\n<@{row['owner_id']}>\n\n"
                            f"**VMID**\n`{row['vmid']}`\n\n"
                            f"**CPU Usage**\n`{cpu:.1f}%`\n\n"
                            f"**RAM Usage**\n`{ram:.1f}%`\n\n"
                            f"**Threshold**\nCPU `{cpu_threshold:.0f}%` • RAM `{ram_threshold:.0f}%`\n\n"
                            f"**Overload**\n{overload}"
                        )
                    await send_log(self.bot, content=text)
                except Exception:
                    log.exception("autoscan failed for VPS %s", row["vmid"])
        except Exception:
            log.exception("autoscan worker failed")

    @worker.before_loop
    async def before_worker(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(AutoScan(bot))
