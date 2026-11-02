import asyncio
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from proxmox_client import PVEError
from utils import (CPU, DISK, EXPIRY_HELP, RAM, ConfirmView, ExpiryError, admin_only, dm,
                   fmt_expiry, parse_expiry, provision_vps, reactivate, resolve_expiry,
                   ram_gb_to_mb, ram_mb_to_gb, vps_autocomplete, vps_embed)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ───────────── /create ─────────────
    @app_commands.command(name="create", description="Naya LXC VPS banao (admin)")
    @app_commands.describe(user="VPS kiska hoga", ram="RAM in GB (e.g. 8 = 8 GB)", cpu="vCPU cores",
                           disk="Disk in GB", expiry=EXPIRY_HELP, hostname="Optional hostname")
    @admin_only()
    async def create(self, interaction: discord.Interaction, user: discord.User,
                     ram: RAM, cpu: CPU, disk: DISK,
                     expiry: Optional[str] = None, hostname: Optional[str] = None):
        try:
            expires_at = resolve_expiry(parse_expiry(expiry))
        except ExpiryError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        class CreateOSView(discord.ui.View):
            def __init__(view_self):
                super().__init__(timeout=120)
                view_self.owner_id = interaction.user.id
                view_self.select = discord.ui.Select(
                    placeholder="OS select karo…",
                    options=[
                        discord.SelectOption(label="Ubuntu 20.04 LTS", value="ubuntu:20.04"),
                        discord.SelectOption(label="Ubuntu 22.04 LTS", value="ubuntu:22.04"),
                        discord.SelectOption(label="Ubuntu 24.04 LTS", value="ubuntu:24.04"),
                        discord.SelectOption(label="Debian 12", value="images:debian/bookworm"),
                    ])
                view_self.select.callback = view_self.selected
                view_self.add_item(view_self.select)

            async def interaction_check(view_self, i):
                if i.user.id != view_self.owner_id:
                    await i.response.send_message("❌ Ye OS selector tumhare liye nahi hai.", ephemeral=True)
                    return False
                return True

            async def selected(view_self, i):
                image = i.data.get("values", [None])[0]
                labels = {
                    "ubuntu:20.04": "Ubuntu 20.04 LTS",
                    "ubuntu:22.04": "Ubuntu 22.04 LTS",
                    "ubuntu:24.04": "Ubuntu 24.04 LTS",
                    "images:debian/bookworm": "Debian 12",
                }
                label = labels.get(image, image)
                await i.response.defer(ephemeral=True, thinking=True)
                msg = await i.followup.send("🛠️ **Creating VPS**\n\n⏳ Preparing container…", ephemeral=True, wait=True)

                async def animate():
                    frames = [
                        "🛠️ **Creating VPS**\n\n⏳ Preparing container…",
                        "🛠️ **Creating VPS**\n\n⏳ Downloading OS image…",
                        "🛠️ **Creating VPS**\n\n⏳ Creating LXC container…",
                        "🛠️ **Creating VPS**\n\n⏳ Configuring CPU / RAM / Disk…",
                        "🛠️ **Creating VPS**\n\n⏳ Installing SSH…",
                        "🛠️ **Creating VPS**\n\n⏳ Finalizing VPS…",
                    ]
                    idx = 0
                    try:
                        while True:
                            await asyncio.sleep(1.5)
                            idx = (idx + 1) % len(frames)
                            await msg.edit(content=frames[idx])
                    except (asyncio.CancelledError, discord.NotFound, discord.HTTPException):
                        return

                anim = asyncio.create_task(animate())
                try:
                    async with self.bot.create_lock:
                        row, password = await provision_vps(self.bot, user.id, ram_gb_to_mb(ram), cpu, disk,
                                                            expires_at, hostname, image=image)
                except PVEError as e:
                    anim.cancel(); await asyncio.gather(anim, return_exceptions=True)
                    return await msg.edit(content=f"❌ VPS ban nahi paya: `{e}`")
                finally:
                    if not anim.done(): anim.cancel()
                    await asyncio.gather(anim, return_exceptions=True)

                embed = vps_embed(row)
                embed.add_field(name="🖥️ OS", value=label)
                embed.add_field(name="🔑 Login", value=f"`root` / `{password}`", inline=False)
                dm_ok = await dm(self.bot, user.id,
                                 "✅ Tumhara VPS ready hai! Password safe rakho. `/manage` se control karo.", embed)
                note = "📩 User ko DM bhej diya." if dm_ok else "⚠️ User ke DMs closed hain — credentials khud do."
                await msg.edit(content=f"✅ VPS created. {note}", embed=embed)
                view_self.stop()

        embed = discord.Embed(title="🖥️ Create VPS", description=(
            f"Owner: {user.mention}\n"
            f"Specs: **{cpu} vCPU • {ram} GB RAM • {disk} GB**\n\n"
            "Neeche operating system select karo."))
        await interaction.response.send_message(embed=embed, view=CreateOSView(), ephemeral=True)

    # ───────────── /editvps ─────────────
    @app_commands.command(name="editvps", description="VPS ke specs / expiry / owner edit karo (admin)")
    @app_commands.describe(vmid="VPS ID", ram="Naya RAM in GB (e.g. 8 = 8 GB)", cpu="Naye vCPU",
                           disk="Naya disk (GB) — sirf badha sakte ho",
                           expiry=f"Aaj se naya expiry: {EXPIRY_HELP}. Skip = unchanged, 'permanent' = no expiry",
                           owner="Naya owner")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @admin_only()
    async def editvps(self, interaction: discord.Interaction, vmid: int,
                      ram: Optional[RAM] = None, cpu: Optional[CPU] = None,
                      disk: Optional[DISK] = None, expiry: Optional[str] = None,
                      owner: Optional[discord.User] = None):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        if all(v is None for v in (ram, cpu, disk, expiry, owner)):
            return await interaction.response.send_message("ℹ️ Kuch change karne ko diya hi nahi.", ephemeral=True)
        if disk is not None and disk <= row["disk_gb"]:
            return await interaction.response.send_message(
                f"❌ Disk sirf increase ho sakti hai (current {row['disk_gb']} GB).", ephemeral=True)

        changes: dict = {}
        if expiry is not None:
            try:
                changes["expires_at"] = resolve_expiry(parse_expiry(expiry))  # from now
            except ExpiryError as e:
                return await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            changes["warned"] = 0

        await interaction.response.defer(ephemeral=True)
        try:
            if ram is not None or cpu is not None:
                await self.bot.pve.set_resources(vmid, cores=cpu, memory_mb=ram_gb_to_mb(ram) if ram is not None else None)
            if disk is not None:
                await self.bot.pve.resize_disk(vmid, disk)
        except PVEError as e:
            return await interaction.followup.send(f"❌ Proxmox error: `{e}`", ephemeral=True)

        if ram is not None: changes["ram_mb"] = ram_gb_to_mb(ram)
        if cpu is not None: changes["cpu"] = cpu
        if disk is not None: changes["disk_gb"] = disk
        if owner is not None: changes["owner_id"] = owner.id
        await self.bot.db.update_vps(vmid, **changes)

        warn = ""
        row = await self.bot.db.get_vps(vmid)
        if row["status"] == "suspended" and "expires_at" in changes:
            err = await reactivate(self.bot, vmid)
            warn = f"\n⚠️ Start failed: `{err}`" if err else "\n▶️ VPS unsuspend + start ho gaya."
            row = await self.bot.db.get_vps(vmid)
        await interaction.followup.send(f"✅ VPS `{vmid}` updated.{warn}", embed=vps_embed(row), ephemeral=True)

    # ───────────── /suspend ─────────────
    @app_commands.command(name="suspend", description="User ka VPS suspend karo (admin)")
    @app_commands.describe(user="VPS owner", vmid="VPS ID")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @admin_only()
    async def suspend(self, interaction: discord.Interaction, user: discord.User, vmid: int):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        if row["owner_id"] != user.id:
            return await interaction.response.send_message(
                f"❌ VPS `{vmid}` ka owner {user.mention} nahi hai. Actual owner: <@{row['owner_id']}>.",
                ephemeral=True)
        if row["status"] == "suspended":
            return await interaction.response.send_message(
                f"ℹ️ VPS `{vmid}` pehle se suspended hai.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.pve.stop(vmid)
        except PVEError as e:
            return await interaction.followup.send(f"❌ VPS stop nahi hua: `{e}`", ephemeral=True)

        await self.bot.db.update_vps(vmid, status="suspended")
        await dm(self.bot, user.id,
                  f"⏸️ Tumhara VPS `{vmid}` admin ke dwara suspend kar diya gaya hai.")
        await interaction.followup.send(
            f"⏸️ VPS `{vmid}` suspended.\n\nOwner: {user.mention}", ephemeral=True)

    # ───────────── /renew ─────────────
    @app_commands.command(name="renew", description="VPS renew karo (admin)")
    @app_commands.describe(vmid="VPS ID",
                           expiry=f"Duration current expiry me add hoti hai. {EXPIRY_HELP}")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @admin_only()
    async def renew(self, interaction: discord.Interaction, vmid: int, expiry: Optional[str] = None):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        try:
            # blank => permanent. Duration extends from current expiry if it's still in the future.
            new_ts = resolve_expiry(parse_expiry(expiry), base=row["expires_at"])
        except ExpiryError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        await self.bot.db.update_vps(vmid, expires_at=new_ts, warned=0)
        extra = ""
        if row["status"] == "suspended":
            err = await reactivate(self.bot, vmid)
            extra = f"\n⚠️ Start failed: `{err}`" if err else "\n▶️ VPS wapas start ho gaya."
        await dm(self.bot, row["owner_id"],
                 f"🔁 VPS `{vmid}` renew ho gaya. Naya expiry: {fmt_expiry(new_ts)}")
        row = await self.bot.db.get_vps(vmid)
        await interaction.followup.send(f"✅ Renewed.{extra}", embed=vps_embed(row), ephemeral=True)

    # ───────────── /deletevps ─────────────
    @app_commands.command(name="deletevps", description="VPS permanently delete karo (admin)")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @admin_only()
    async def deletevps(self, interaction: discord.Interaction, vmid: int):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ VPS `{vmid}` ({row['hostname']}) **permanently delete** hoga. Pakka?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            return await interaction.edit_original_response(content="Cancelled.", view=None)
        try:
            await self.bot.pve.delete(vmid)
        except PVEError as e:
            return await interaction.edit_original_response(content=f"❌ `{e}`", view=None)
        await self.bot.db.update_vps(vmid, status="deleted")
        await self.bot.db.clear_shares(vmid)
        await interaction.edit_original_response(content=f"🗑️ VPS `{vmid}` delete ho gaya.", view=None)

    # ───────────── /listvps ─────────────
    @app_commands.command(name="listvps", description="Saare VPS ki list (admin)")
    @admin_only()
    async def listvps(self, interaction: discord.Interaction):
        rows = await self.bot.db.list_vps()
        if not rows:
            return await interaction.response.send_message("Koi VPS nahi hai.", ephemeral=True)
        lines = []
        for r in rows[:40]:
            icon = "🟢" if r["status"] == "active" else "🔴"
            lines.append(f"{icon} `{r['vmid']}` **{r['hostname']}** — <@{r['owner_id']}> • "
                         f"{r['cpu']}c/{ram_mb_to_gb(r['ram_mb'])}GB/{r['disk_gb']}GB • {fmt_expiry(r['expires_at'])}")
        text = "\n".join(lines)[:4000]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"📋 VPS List ({len(rows)})", description=text), ephemeral=True)

    # ───────────── /myvps (everyone) ─────────────
    @app_commands.command(name="myvps", description="Tumhare VPS ki list")
    async def myvps(self, interaction: discord.Interaction):
        rows = await self.bot.db.accessible_vps(interaction.user.id)
        if not rows:
            return await interaction.response.send_message("Tumhare paas koi VPS nahi hai.", ephemeral=True)
        lines = [f"`{r['vmid']}` **{r['hostname']}** — {r['cpu']}c/{ram_mb_to_gb(r['ram_mb'])}GB/{r['disk_gb']}GB • "
                 f"{fmt_expiry(r['expires_at'])}" + ("" if r["owner_id"] == interaction.user.id else " (shared)")
                 for r in rows]
        await interaction.response.send_message(
            embed=discord.Embed(title="🖥️ My VPS", description="\n".join(lines)[:4000]), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
