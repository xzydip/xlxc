import asyncio
import ipaddress
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from host import HostError as DetectorError, HostShell
from config import cfg
from proxmox_client import PVEError, lxc_name
from utils import get_accessible, is_expired, is_admin, vps_autocomplete, vps_embed, gen_password, ram_mb_to_gb
from tailscale import TailscaleAPIError, find_ipv4

log = logging.getLogger("pvebot.manage")

NEEDS_ACTIVE = ("Start", "Restart")     # disabled when VPS is expired / suspended
NEEDS_RUNNING = ("SSH", "SSHX")         # disabled unless VPS is active AND running
SSHX_URL_RE = re.compile(r'https://sshx\.io/s/[^\s"<>]+')
_SSHX_BUSY: set[int] = set()
_TAILSCALE_BUSY: set[int] = set()

OS_OPTIONS = [
    ("Ubuntu 20.04 LTS", "ubuntu:20.04"),
    ("Ubuntu 22.04 LTS", "ubuntu:22.04"),
    ("Ubuntu 24.04 LTS", "ubuntu:24.04"),
    ("Debian 12", "images:debian/bookworm"),
]

def os_label(image: str | None) -> str:
    return next((n for n, v in OS_OPTIONS if v == image), image or "Unknown")

SSHX_SCRIPT = r'''lxc exec __VMID__ -- bash -lc '
set -u
LOG=/tmp/sshx-bot.log
rm -f "$LOG"
export DEBIAN_FRONTEND=noninteractive
if ! command -v curl >/dev/null 2>&1; then
  apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq curl ca-certificates >/dev/null 2>&1 || {
    echo "E|container me curl install nahi hua"; exit 0;
  }
fi
# Start SSHX inside the selected container and keep it alive after lxc exec exits.
nohup bash -lc "curl -sSf https://sshx.io/get | sh -s run" >"$LOG" 2>&1 </dev/null &
PID=$!
for i in $(seq 1 45); do
  U=$(grep -oE 'https://sshx\.io/s/[A-Za-z0-9_./?=&:-]+' "$LOG" 2>/dev/null | head -n1 || true)
  if [ -n "$U" ]; then
    echo "URL|$U"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    break
  fi
  sleep 1
done
U=$(grep -oE 'https://sshx\.io/s/[A-Za-z0-9_./?=&:-]+' "$LOG" 2>/dev/null | head -n1 || true)
if [ -n "$U" ]; then
  echo "URL|$U"
else
  ERR=$(tail -n 12 "$LOG" 2>/dev/null | tr "\n" " " | cut -c1-500)
  echo "E|SSHX link generate nahi hua. ${ERR:-container se koi output nahi mila}"
fi
' </dev/null
'''



class RebuildSelectView(discord.ui.View):
    def __init__(self, bot, vmid: int, user_id: int):
        super().__init__(timeout=120)
        self.bot, self.vmid, self.user_id = bot, vmid, user_id
        select = discord.ui.Select(
            placeholder="Naya OS select karo…",
            options=[discord.SelectOption(label=n, value=v) for n, v in OS_OPTIONS]
        )
        select.callback = self.selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ye rebuild selector tumhare liye nahi hai.", ephemeral=True)
            return False
        return True

    async def selected(self, interaction: discord.Interaction):
        image = interaction.data.get("values", [None])[0]
        label = os_label(image)
        row = await self.bot.db.get_vps(self.vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        confirm = discord.ui.View(timeout=30)
        result = {"ok": None}
        async def check(i: discord.Interaction):
            return i.user.id == self.user_id
        yes = discord.ui.Button(label="Rebuild", emoji="♻️", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def ycb(i):
            result["ok"] = True
            await i.response.defer(ephemeral=True)
            confirm.stop()
        async def ncb(i):
            result["ok"] = False
            await i.response.defer(ephemeral=True)
            confirm.stop()
        yes.callback = ycb; no.callback = ncb
        confirm.add_item(yes); confirm.add_item(no)
        await interaction.followup.send(
            f"⚠️ **Rebuild VPS `{self.vmid}`?**\n\n"
            f"New OS: **{label}**\n"
            "⚠️ Existing VPS data will be permanently erased. New root password will be generated.",
            view=confirm, ephemeral=True)
        await confirm.wait()
        if not result["ok"]:
            return
        async with self.bot.create_lock:
            password = gen_password()
            try:
                await self.bot.pve.rebuild(self.vmid, row["hostname"], row["cpu"], row["ram_mb"], row["disk_gb"], password, image)
            except PVEError as e:
                return await interaction.followup.send(f"❌ Rebuild failed: `{e}`", ephemeral=True)
            await self.bot.db.update_vps(self.vmid, root_password=password, os_image=image, status="active", warned=0)
        updated = await self.bot.db.get_vps(self.vmid)
        embed = vps_embed(updated)
        embed.add_field(name="🖥️ OS", value=label)
        embed.add_field(name="🔑 Login", value=f"`root` / `{password}`", inline=False)
        await interaction.followup.send("✅ Rebuild complete. New credentials:", embed=embed, ephemeral=True)
        try:
            await interaction.user.send(f"♻️ VPS `{self.vmid}` rebuilt with **{label}**.\nUsername: `root`\nPassword: `{password}`\nSSH port: `22`")
        except discord.Forbidden:
            pass
        self.stop()


class SSHChoiceView(discord.ui.View):
    def __init__(self, manage_view: "ManageView"):
        super().__init__(timeout=90)
        self.manage_view = manage_view

    @discord.ui.button(label="Public IP", emoji="🌐", style=discord.ButtonStyle.primary)
    async def public_ssh(self, interaction: discord.Interaction, _):
        await self.manage_view.send_ssh_details(interaction, "public")
        self.stop()

    @discord.ui.button(label="Private IP", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def private_ssh(self, interaction: discord.Interaction, _):
        await self.manage_view.send_ssh_details(interaction, "private")
        self.stop()


class PasswordView(discord.ui.View):
    def __init__(self, manage_view: "ManageView", password: str):
        super().__init__(timeout=180)
        self.manage_view = manage_view
        self.password = password

    @discord.ui.button(label="Regen Pass", emoji="🔄", style=discord.ButtonStyle.danger)
    async def regen_pass(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.manage_view.bot.db.get_vps(self.manage_view.vmid)
        if not row:
            return await interaction.followup.send("❌ VPS exist nahi karta.", ephemeral=True)
        try:
            live = await self.manage_view.bot.pve.status(self.manage_view.vmid)
            if live.get("state") != "running":
                return await interaction.followup.send("ℹ️ VPS running nahi hai — pehle **Start** karo.", ephemeral=True)
            password = gen_password()
            await self.manage_view.bot.pve.set_root_password(self.manage_view.vmid, password)
            await self.manage_view.bot.db.update_vps(self.manage_view.vmid, root_password=password)
        except PVEError as e:
            return await interaction.followup.send(f"❌ Password regenerate nahi hua: `{e}`", ephemeral=True)

        self.password = password
        embed = discord.Embed(
            title="🔐 VPS Password",
            description=f"`vps-{self.manage_view.vmid}`",
            color=discord.Color.green(),
        )
        embed.add_field(name="Current Password", value=f"`{password}`", inline=False)
        embed.add_field(name="Username", value="`root`", inline=True)
        embed.add_field(name="SSH Port", value=f"`{row['ssh_port'] or 22}`", inline=True)
        await interaction.followup.send(embed=embed, view=self, ephemeral=True)


class ManageView(discord.ui.View):
    def __init__(self, bot, vmid: int, shell: HostShell):
        super().__init__(timeout=300)
        self.bot, self.vmid, self.shell = bot, vmid, shell

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await get_accessible(self.bot, interaction.user.id, self.vmid):
            return True
        await interaction.response.send_message("❌ Tumhara access nahi hai.", ephemeral=True)
        return False

    def sync_buttons(self, row, live: dict | None):
        usable = row["status"] == "active" and not is_expired(row)
        running = bool(live and live["state"] == "running")
        for child in self.children:
            if child.label in NEEDS_ACTIVE:
                child.disabled = not usable
            elif child.label in NEEDS_RUNNING:
                child.disabled = not (usable and running)

    async def render(self, interaction: discord.Interaction, note: str | None = None):
        row = await self.bot.db.get_vps(self.vmid)
        if not row:
            return await interaction.edit_original_response(content="❌ VPS exist nahi karta.", embed=None, view=None)
        try:
            live = await self.bot.pve.status(self.vmid)
        except PVEError:
            live = None
        self.sync_buttons(row, live)
        shares = await self.bot.db.get_shares(self.vmid)
        await interaction.edit_original_response(content=note, embed=vps_embed(row, live, shares), view=self)

    async def _act(self, interaction: discord.Interaction, action: str, label: str):
        await interaction.response.defer()
        row = await self.bot.db.get_vps(self.vmid)
        if action in ("start", "reboot") and row and (row["status"] != "active" or is_expired(row)):
            return await self.render(interaction, "⛔ VPS expired hai — admin se renew karwao.")
        try:
            if action == "start":
                await self.bot.pve.start(self.vmid)
            elif action == "stop":
                await self.bot.pve.stop(self.vmid)
            elif action == "reboot":
                await self.bot.pve.reboot(self.vmid)
            note = f"✅ {label} done."
        except PVEError as e:
            note = f"❌ `{e}`"
        await self.render(interaction, note)

    async def _live_if_usable(self, interaction: discord.Interaction):
        """(row, live) if VPS is active + running, else replies with the reason and returns None."""
        row = await self.bot.db.get_vps(self.vmid)
        if not row or row["status"] != "active" or is_expired(row):
            await interaction.followup.send("⛔ VPS active nahi hai (expired / suspended).", ephemeral=True)
            return None
        try:
            live = await self.bot.pve.status(self.vmid)
        except PVEError as e:
            await interaction.followup.send(f"❌ `{e}`", ephemeral=True)
            return None
        if live["state"] != "running":
            await interaction.followup.send("ℹ️ VPS running nahi hai — pehle **Start** karo.", ephemeral=True)
            return None
        return row, live

    # ───────────── row 0: power ─────────────
    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def start(self, interaction, _):
        await self._act(interaction, "start", "Start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction, _):
        await self._act(interaction, "stop", "Stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def restart(self, interaction, _):
        await self._act(interaction, "reboot", "Restart")

    # ───────────── row 1: access ─────────────
    @discord.ui.button(label="SSH", emoji="🔑", style=discord.ButtonStyle.secondary, row=1)
    async def ssh(self, interaction: discord.Interaction, _):
        if not await self._live_if_usable(interaction):
            return
        await interaction.response.send_message(
            "🔐 **SSH Connection**\n\nSelect connection type:",
            view=SSHChoiceView(self), ephemeral=True)

    async def _public_ip(self, vmid: int) -> str | None:
        name = lxc_name(vmid)
        script = """set -u
if command -v curl >/dev/null 2>&1; then
  curl -4 -fsS --max-time 8 https://api.ipify.org || true
elif command -v wget >/dev/null 2>&1; then
  wget -qO- --timeout=8 https://api.ipify.org || true
fi
"""
        try:
            import shlex
            raw = await self.shell.run(f"lxc exec {name} -- bash -lc {shlex.quote(script)}", timeout=15)
        except Exception:
            return None
        for line in raw.splitlines():
            value = line.strip()
            try:
                addr = ipaddress.ip_address(value)
                if addr.version == 4 and not addr.is_private:
                    return value
            except ValueError:
                continue
        return None

    async def send_ssh_details(self, interaction: discord.Interaction, kind: str):
        await interaction.response.defer(ephemeral=True)
        got = await self._live_if_usable(interaction)
        if not got:
            return
        row, _live = got
        port = int(row["ssh_port"] or 22)
        if kind == "public":
            ip = await self._public_ip(self.vmid)
            label = "🌐 SSH • Public"
            if not ip:
                return await interaction.followup.send("❌ Public IP detect nahi hua.", ephemeral=True)
        else:
            ip = await self.bot.pve.tailscale_ip(self.vmid)
            label = "🔒 SSH • Private"
            if not ip:
                return await interaction.followup.send("❌ Private IP configured nahi hai. Pehle Private IP setup karo.", ephemeral=True)
        e = discord.Embed(title="Connection information", description=f"`{lxc_name(self.vmid)}`", color=discord.Color.blurple())
        e.add_field(name=label, value=f"Port: `{port}`\nProtocol: `TCP`\nService: `OpenSSH`", inline=False)
        e.add_field(name="SSH Command", value=f"`ssh root@{ip} -p {port}`", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="SSHX", emoji="🌐", style=discord.ButtonStyle.primary, row=1)
    async def sshx(self, interaction: discord.Interaction, _):
        if self.vmid in _SSHX_BUSY:
            return await interaction.response.send_message("⏳ SSHX session already start ho raha hai…", ephemeral=True)
        _SSHX_BUSY.add(self.vmid)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if not await self._live_if_usable(interaction):
                return
            try:
                raw = await self.shell.run(SSHX_SCRIPT.replace("__VMID__", lxc_name(self.vmid)), timeout=45)
            except DetectorError as e:
                return await interaction.followup.send(f"❌ Host/LXD command fail: `{e}`", ephemeral=True)
            url = None
            err = None
            for line in raw.splitlines():
                kind, sep, rest = line.partition("|")
                if kind == "URL" and sep and SSHX_URL_RE.match(rest.strip()):
                    url = rest.strip(); break
                if kind == "E" and sep:
                    err = rest.strip()
            if not url:
                log.warning("SSHX failed for VPS %s: %s | raw=%r", self.vmid, err, raw[:1000])
                return await interaction.followup.send(f"❌ SSHX generate nahi hua: `{err or 'link output nahi mila'}`", ephemeral=True)
            link = discord.ui.View(timeout=300)
            link.add_item(discord.ui.Button(label="Open SSHX Terminal", emoji="🌐", url=url))
            embed = discord.Embed(
                title="🔗 SSHX Access",
                description=(
                    "Web SSH connection for VPS\n\n"
                    f"`vps-{self.vmid}`"
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(name="🔗 Link", value="[Click to Open Terminal]({})".format(url), inline=False)
            embed.add_field(
                name="⚠️ Security",
                value="This link grants direct root access. Do not share it.",
                inline=False,
            )
            await interaction.followup.send(embed=embed, view=link, ephemeral=True)
        finally:
            _SSHX_BUSY.discard(self.vmid)

    @discord.ui.button(label="Private IP", emoji="🔒", style=discord.ButtonStyle.primary, row=2)
    async def private_ip(self, interaction: discord.Interaction, _):
        if self.vmid in _TAILSCALE_BUSY:
            return await interaction.response.send_message(
                "⏳ Tailscale authorization already start ho chuka hai. Apne DM check karo.", ephemeral=True
            )
        _TAILSCALE_BUSY.add(self.vmid)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            row = await self.bot.db.get_vps(self.vmid)
            if not row or row["status"] != "active" or is_expired(row):
                return await interaction.followup.send("⛔ VPS active nahi hai (expired / suspended).", ephemeral=True)
            try:
                live = await self.bot.pve.status(self.vmid)
            except PVEError as e:
                return await interaction.followup.send(f"❌ `{e}`", ephemeral=True)
            if live["state"] != "running":
                return await interaction.followup.send("ℹ️ VPS running nahi hai — pehle **Start** karo.", ephemeral=True)

            try:
                url, current_ip = await self.bot.pve.tailscale_authorize(self.vmid)
            except PVEError as e:
                return await interaction.followup.send(
                    f"❌ Tailscale setup fail: `{e}`\n\n"
                    "LXD host par `/dev/net/tun` available hona chahiye.", ephemeral=True
                )

            if current_ip:
                await self._send_private_ip(interaction.user, current_ip, via_api=False)
                return await interaction.followup.send(f"✅ Private Tailscale IP ready: `{current_ip}` — DM bhej diya.", ephemeral=True)

            if not url:
                return await interaction.followup.send(
                    "❌ Tailscale authorization link generate nahi hua.", ephemeral=True
                )

            link_view = discord.ui.View()
            link_view.add_item(discord.ui.Button(label="Authorize Tailscale", emoji="🔐", url=url))
            embed = discord.Embed(
                title="🔐 Private IP Authorization",
                description=(
                    f"Authorize the private IP connection for `vps-{self.vmid}`.\n\n"
                    "After authorization, the VPS `100.x.x.x` private IP will be sent automatically."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="⚠️ Security",
                value="Authorize only with your own Tailscale account.",
                inline=False,
            )
            try:
                await interaction.user.send(embed=embed, view=link_view)
                dm_ok = True
            except discord.Forbidden:
                await interaction.followup.send(embed=embed, view=link_view, ephemeral=True)
                dm_ok = False

            asyncio.create_task(self._wait_for_tailscale_ip(interaction.user, self.vmid))
            await interaction.followup.send(
                "📩 Authorization link DM kar diya." if dm_ok else "📩 DM disabled hai, link yahin diya hai.",
                ephemeral=True
            )
        finally:
            _TAILSCALE_BUSY.discard(self.vmid)

    async def _send_private_ip(self, user: discord.abc.User, ip: str, via_api: bool = False):
        embed = discord.Embed(
            title="🌐 Private IP Ready",
            description=(
                f"VPS: `vps-{self.vmid}`\n\n"
                f"Private IP: `{ip}`\n\n"
                f"SSH: `ssh root@{ip}`"
            ),
            color=discord.Color.blurple(),
        )
        if via_api:
            embed.add_field(name="Verification", value="IP verified.", inline=False)
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _wait_for_tailscale_ip(self, user: discord.abc.User, vmid: int):
        # Give the user up to 5 minutes to finish the Tailscale browser authorization.
        hostname = f"xzy-vps-{int(vmid)}"
        for _ in range(60):
            await asyncio.sleep(5)
            ip = None
            via_api = False
            if cfg.tailscale_api_key:
                try:
                    ip = await asyncio.to_thread(find_ipv4, hostname)
                    via_api = bool(ip)
                except TailscaleAPIError:
                    ip = None
            if not ip:
                ip = await self.bot.pve.tailscale_ip(vmid)
            if ip:
                await self._send_private_ip(user, ip, via_api=via_api)
                return
        try:
            await user.send(f"⏳ VPS `{vmid}` Tailscale authorization 5 minutes me complete nahi hua. `/manage {vmid}` se **Private IP** dobara press karo.")
        except discord.Forbidden:
            pass

    @discord.ui.button(label="Network & Ports", emoji="🌐", style=discord.ButtonStyle.primary, row=2)
    async def network_ports(self, interaction: discord.Interaction, _):
        await interaction.response.defer(ephemeral=True)
        got = await self._live_if_usable(interaction)
        if not got:
            return
        row, _live = got
        public_ip = await self._public_ip(self.vmid)
        private_ip = await self.bot.pve.tailscale_ip(self.vmid)
        private_ip = private_ip or "Not configured"
        public_ip = public_ip or "Not detected"
        port = int(row["ssh_port"] or 22)
        e = discord.Embed(title="🌐 Network & Ports", description=f"`{lxc_name(self.vmid)}`", color=discord.Color.blurple())
        e.add_field(name="Public IP", value=f"`{public_ip}`", inline=True)
        e.add_field(name="Private IP", value=f"`{private_ip}`", inline=True)
        e.add_field(name="🔐 SSH • Public", value=f"`root@{public_ip}`\nPort: `{port}`\nProtocol: `TCP`\nService: `OpenSSH`", inline=False)
        e.add_field(name="🔒 SSH • Private", value=f"`root@{private_ip}`\nPort: `{port}`\nProtocol: `TCP`\nService: `OpenSSH`", inline=False)
        e.add_field(name="🔗 Ports", value=f"`{port}` — SSH\nOther application ports can be listened to inside the VPS.", inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="Password", emoji="🔑", style=discord.ButtonStyle.secondary, row=3)
    async def password_btn(self, interaction: discord.Interaction, _):
        row = await self.bot.db.get_vps(self.vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS exist nahi karta.", ephemeral=True)
        password = row["root_password"] or "Not available"
        embed = discord.Embed(
            title="🔐 VPS Password",
            description=f"`vps-{self.vmid}`",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Current Password", value=f"`{password}`", inline=False)
        embed.add_field(name="Username", value="`root`", inline=True)
        embed.add_field(name="SSH Port", value=f"`{row['ssh_port'] or 22}`", inline=True)
        await interaction.response.send_message(embed=embed, view=PasswordView(self, password), ephemeral=True)

    @discord.ui.button(label="Rebuild", emoji="♻️", style=discord.ButtonStyle.danger, row=3)
    async def rebuild_btn(self, interaction: discord.Interaction, _):
        if not await get_accessible(self.bot, interaction.user.id, self.vmid):
            return await interaction.response.send_message("❌ Tumhara access nahi hai.", ephemeral=True)
        await interaction.response.send_message(
            f"♻️ VPS `{self.vmid}` rebuild karne ke liye new OS select karo. **Current data delete ho jayega.**",
            view=RebuildSelectView(self.bot, self.vmid, interaction.user.id), ephemeral=True)

    @discord.ui.button(label="Refresh", emoji="🔃", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction, _):
        await interaction.response.defer()
        await self.render(interaction)



class ManageSelectView(discord.ui.View):
    """VPS selector shown by /manage. Only the invoking user can use it."""
    def __init__(self, bot, user_id: int, rows):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        options = [
            discord.SelectOption(
                label=f"VPS {r['vmid']}",
                description=f"{r['hostname']} • {r['cpu']} vCPU • {ram_mb_to_gb(r['ram_mb'])} GB",
                value=str(r['vmid']),
                emoji="🖥️",
            ) for r in rows[:25]
        ]
        select = discord.ui.Select(placeholder="Apna VPS select karo…", options=options)
        select.callback = self.selected
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ye VPS selector tumhare liye nahi hai.", ephemeral=True)
            return False
        return True

    async def selected(self, interaction: discord.Interaction):
        vmid = int(interaction.data.get("values", [0])[0])
        row = await get_accessible(self.bot, interaction.user.id, vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila ya access nahi hai.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        shell = self.bot.pve.shell
        view = ManageView(self.bot, vmid, shell)
        try:
            live = await self.bot.pve.status(vmid)
        except PVEError:
            live = None
        view.sync_buttons(row, live)
        shares = await self.bot.db.get_shares(vmid)
        await interaction.followup.send(embed=vps_embed(row, live, shares), view=view, ephemeral=True)
        self.stop()

class Manage(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.shell = bot.pve.shell

    @app_commands.command(name="manage", description="Apna VPS select karke management panel kholo")
    async def manage(self, interaction: discord.Interaction):
        if is_admin(interaction.user.id):
            rows = await self.bot.db.list_vps()
        else:
            rows = await self.bot.db.accessible_vps(interaction.user.id)
        if not rows:
            return await interaction.response.send_message("ℹ️ Tumhare paas koi VPS nahi hai.", ephemeral=True)
        embed = discord.Embed(
            title="🖥️ VPS Management",
            description="Neeche se VPS select karo. Select karne ke baad us VPS ka management panel open hoga.",
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{len(rows)} VPS available")
        await interaction.response.send_message(
            embed=embed,
            view=ManageSelectView(self.bot, interaction.user.id, rows),
            ephemeral=True,
        )


    @app_commands.command(name="system", description="Main VPS ke specs aur CPU/RAM/storage usage dikhao")
    @app_commands.check(is_admin)
    async def system_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        script = r'''
set -u
cpu_count=$(nproc 2>/dev/null || echo 0)
read -r _ u n s i w irq sirq steal _ < /proc/stat
prev_idle=$((i+w)); prev_total=$((u+n+s+i+w+irq+sirq+steal))
sleep 0.5
read -r _ u n s i w irq sirq steal _ < /proc/stat
idle=$((i+w)); total=$((u+n+s+i+w+irq+sirq+steal))
dt=$((total-prev_total)); di=$((idle-prev_idle))
if [ "$dt" -gt 0 ]; then cpu_pct=$(awk -v a="$dt" -v b="$di" 'BEGIN {printf "%.1f", (1-b/a)*100}'); else cpu_pct="0.0"; fi
mem_total=$(awk '/^MemTotal:/ {print $2*1024}' /proc/meminfo)
mem_avail=$(awk '/^MemAvailable:/ {print $2*1024}' /proc/meminfo)
mem_used=$((mem_total-mem_avail))
read -r disk_total disk_used disk_avail disk_pct <<EOF
$(df -B1 / | awk 'NR==2 {gsub("%","",$5); print $2,$3,$4,$5}')
EOF
uptime_sec=$(cut -d. -f1 /proc/uptime)
hostname=$(hostname)
printf 'CPU_COUNT=%s\nCPU_USED=%s\nMEM_TOTAL=%s\nMEM_USED=%s\nDISK_TOTAL=%s\nDISK_USED=%s\nDISK_AVAIL=%s\nDISK_PCT=%s\nUPTIME=%s\nHOSTNAME=%s\n' "$cpu_count" "$cpu_pct" "$mem_total" "$mem_used" "$disk_total" "$disk_used" "$disk_avail" "$disk_pct" "$uptime_sec" "$hostname"
'''
        try:
            raw = await self.bot.pve.shell.run(script, timeout=15)
        except Exception as e:
            return await interaction.followup.send(f"❌ System usage read nahi hua: `{e}`", ephemeral=True)
        vals = {}
        for line in raw.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = v.strip()
        def gib(v):
            try: return float(v) / (1024**3)
            except: return 0.0
        def uptime(v):
            try:
                sec=int(v); d, sec=divmod(sec,86400); h, sec=divmod(sec,3600); m=sec//60
                return f"{d}d {h}h {m}m"
            except: return "Unknown"
        embed=discord.Embed(title="🖥️ Main VPS System Usage", color=discord.Color.blurple())
        embed.add_field(name="💻 CPU", value=f"Cores: `{vals.get('CPU_COUNT','0')}`\nUsage: `{vals.get('CPU_USED','0')}%`", inline=True)
        embed.add_field(name="🧠 RAM", value=f"Used: `{gib(vals.get('MEM_USED',0)):.2f} GB`\nTotal: `{gib(vals.get('MEM_TOTAL',0)):.2f} GB`", inline=True)
        embed.add_field(name="💾 Storage", value=f"Used: `{gib(vals.get('DISK_USED',0)):.2f} GB`\nTotal: `{gib(vals.get('DISK_TOTAL',0)):.2f} GB`\nUsage: `{vals.get('DISK_PCT','0')}%`", inline=True)
        embed.add_field(name="🖥️ Host", value=f"`{vals.get('HOSTNAME','Unknown')}`\nUptime: `{uptime(vals.get('UPTIME',0))}`", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="suspendlist", description="Suspended VPS ki list dekho")
    @app_commands.check(is_admin)
    async def suspendlist(self, interaction: discord.Interaction):
        rows = [r for r in await self.bot.db.list_vps() if r["status"] == "suspended"]
        if not rows:
            return await interaction.response.send_message("ℹ️ Koi suspended VPS nahi hai.", ephemeral=True)
        lines = [f"`{r['vmid']}` — <@{r['owner_id']}> — {r['hostname']}" for r in rows]
        await interaction.response.send_message("⏸️ **Suspended VPS**\n\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="unsuspend", description="Suspended VPS ko unsuspend aur start karo")
    @app_commands.describe(vmid="Suspended VPS ID")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    @app_commands.check(is_admin)
    async def unsuspend(self, interaction: discord.Interaction, vmid: int):
        row = await self.bot.db.get_vps(vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila.", ephemeral=True)
        if row["status"] != "suspended":
            return await interaction.response.send_message("ℹ️ VPS suspended nahi hai.", ephemeral=True)
        if is_expired(row):
            return await interaction.response.send_message("⛔ VPS expired hai. Pehle `/renew` karo.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.pve.start(vmid)
            await self.bot.db.update_vps(vmid, status="active")
        except PVEError as e:
            return await interaction.followup.send(f"❌ VPS start nahi hua: `{e}`", ephemeral=True)
        await interaction.followup.send(f"▶️ VPS `{vmid}` unsuspended aur started.", ephemeral=True)

    @app_commands.command(name="rebuild", description="VPS ko selected OS se rebuild karo")
    @app_commands.autocomplete(vmid=vps_autocomplete)
    async def rebuild_cmd(self, interaction: discord.Interaction, vmid: int):
        row = await get_accessible(self.bot, interaction.user.id, vmid)
        if not row:
            return await interaction.response.send_message("❌ VPS nahi mila ya access nahi hai.", ephemeral=True)
        await interaction.response.send_message(
            f"♻️ VPS `{vmid}` rebuild karne ke liye OS select karo. **Current data permanently delete hoga.**",
            view=RebuildSelectView(self.bot, vmid, interaction.user.id), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Manage(bot))
