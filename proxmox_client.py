"""LXD/LXC control backend.

The original bot used Proxmox `pct`. This version uses the LXD `lxc` CLI,
so each VPS is a real LXD container managed directly from the VPS host.
The public PVE class name is kept for compatibility with the existing cogs.
"""
import re
import shlex
import time

from config import cfg
from host import HostError, HostShell

_q = shlex.quote
STALE_AFTER = 120


def lxc_name(vmid: int) -> str:
    """Map the numeric bot VPS ID to a valid LXD instance name."""
    return f"vps-{int(vmid)}"


class PVEError(Exception):
    pass


def _image_from_template(value: str) -> str:
    """Convert an old Proxmox template value to a sensible LXD image alias."""
    if not value:
        return "images:ubuntu/22.04"
    if value.startswith("local:vztmpl/"):
        m = re.search(r"(ubuntu)[-_](\d+\.\d+)", value, re.I)
        if m:
            return f"images:{m.group(1).lower()}/{m.group(2)}"
        return "images:ubuntu/22.04"
    return value


def _parse_list(raw: str) -> dict[int, dict]:
    """Parse `lxc list -c n,s,4 --format csv`."""
    out: dict[int, dict] = {}
    for line in raw.splitlines():
        parts = [x.strip() for x in line.split(",", 2)]
        if len(parts) < 2:
            continue
        name, state = parts[0], parts[1]
        m = re.fullmatch(r"vps-(\d+)", name)
        if not m:
            continue
        vmid = int(m.group(1))
        ip = None
        if len(parts) >= 3:
            ipm = re.search(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", parts[2])
            if ipm:
                ip = ipm.group(0)
        out[vmid] = {"name": name, "state": state.lower(), "ip": ip}
    return out


class PVE:
    """Compatibility wrapper exposing the old bot API using LXD commands."""

    def __init__(self):
        self.shell = HostShell()
        self._prev: dict[int, tuple[int, float, float]] = {}

    async def _sh(self, script: str, timeout: int = 60) -> str:
        try:
            rc, out, err = await self.shell.exec(script, timeout)
        except HostError as e:
            raise PVEError(str(e)) from e
        if rc != 0:
            msg = (err or out).strip()[-500:] or f"exit code {rc}"
            raise PVEError(msg)
        return out

    async def _list_raw(self) -> dict[int, dict]:
        raw = await self._sh("command -v lxc >/dev/null 2>&1 && lxc list -c n,s,4 --format csv", timeout=30)
        return _parse_list(raw)

    async def running_vps_count(self) -> int:
        """Return the number of LXD containers created by this bot that are RUNNING."""
        items = await self._list_raw()
        return sum(1 for item in items.values() if item.get("state") == "running")

    async def _state(self, vmid) -> str:
        items = await self._list_raw()
        d = items.get(int(vmid))
        if not d:
            raise PVEError(f"VPS {vmid} does not exist")
        return d["state"]

    async def _sample_container(self, vmid: int, d: dict) -> dict:
        name = d["name"]
        state = d["state"]
        res = {
            "state": state,
            "cpu": 0.0,
            "mem": 0,
            "maxmem": 0,
            "uptime": 0,
            "ip": d.get("ip"),
        }
        if state != "running":
            return res

        # Read cgroup counters from inside the LXD container. This works with
        # both cgroup v1/v2 and does not require Proxmox-specific paths.
        script = r'''set -eu
if [ -f /sys/fs/cgroup/cpu.stat ]; then cat /sys/fs/cgroup/cpu.stat; fi
echo '__MEM__'
if [ -f /sys/fs/cgroup/memory.current ]; then cat /sys/fs/cgroup/memory.current; fi
echo '__MAX__'
if [ -f /sys/fs/cgroup/memory.max ]; then cat /sys/fs/cgroup/memory.max; fi
echo '__UP__'
ps -o etimes= -p 1 2>/dev/null || true
'''
        try:
            raw = await self._sh(f"lxc exec {_q(name)} -- bash -lc {_q(script)}", timeout=30)
        except PVEError:
            return res
        usec = None
        mem = 0
        maxmem = 0
        uptime = 0
        section = "cpu"
        for line in raw.splitlines():
            line = line.strip()
            if line == "__MEM__":
                section = "mem"; continue
            if line == "__MAX__":
                section = "max"; continue
            if line == "__UP__":
                section = "up"; continue
            if section == "cpu" and line.startswith("usage_usec "):
                try: usec = int(line.split()[1])
                except (ValueError, IndexError): pass
            elif section == "mem":
                try: mem = int(line)
                except ValueError: pass
            elif section == "max":
                if line.isdigit():
                    maxmem = int(line)
            elif section == "up":
                try: uptime = int(line)
                except ValueError: pass

        if maxmem <= 0:
            try:
                lim = await self._sh(f"lxc config get {_q(name)} limits.memory", timeout=20)
                m = re.match(r"([0-9]+(?:\.[0-9]+)?)([KMGTP]i?B)?", lim.strip(), re.I)
                if m:
                    mult = {None: 1, "K":1024, "KB":1000, "KiB":1024, "M":2**20, "MB":10**6,
                            "MiB":2**20, "G":2**30, "GB":10**9, "GiB":2**30}.get(m.group(2), 1)
                    maxmem = int(float(m.group(1)) * mult)
            except PVEError:
                pass

        res["mem"] = mem
        res["maxmem"] = maxmem
        res["uptime"] = uptime
        if usec is not None:
            now = time.time()
            if vmid in self._prev:
                old_usec, old_ts, _ = self._prev[vmid]
                dt = now - old_ts
                if dt > 0:
                    # LXD limits.cpu may be a number or a range/list. Use the
                    # first numeric value as the allocated vCPU count.
                    try:
                        lim = await self._sh(f"lxc config get {_q(name)} limits.cpu", timeout=10)
                        cores = max(int(re.search(r"\d+", lim).group(0)), 1)
                    except Exception:
                        cores = 1
                    res["cpu"] = min(max((usec-old_usec) / 1e6 / dt / cores, 0.0), 1.0)
            self._prev[vmid] = (usec, now, now)
        return res

    async def _measure(self, ids: list[int] | None, detail: bool = False, force_two: bool = False) -> dict:
        items = await self._list_raw()
        wanted = list(items) if ids is None else [int(x) for x in ids]
        missing = [x for x in wanted if x not in items]
        if ids is not None and missing:
            raise PVEError(f"VPS {missing[0]} does not exist")
        out = {}
        for vmid in wanted:
            if vmid in items:
                out[vmid] = await self._sample_container(vmid, items[vmid])
        return out

    async def next_vmid(self, start: int) -> int:
        items = await self._list_raw()
        used = set(items)
        for vmid in range(start, start + 5000):
            if vmid not in used:
                return vmid
        raise PVEError("No free VPS ID found")

    async def create(self, vmid, hostname, cores, memory_mb, disk_gb, password, image: str | None = None):
        name = lxc_name(vmid)
        image = _image_from_template(image or cfg.image)
        storage = _q(cfg.storage)
        # LXD names cannot contain spaces; hostname is configured inside the OS.
        storage_arg = f" --storage {storage}" if cfg.storage else ""
        cmd = (
            f"lxc launch {_q(image)} {_q(name)}{storage_arg} "
            f"-c limits.cpu={int(cores)} -c limits.memory={int(memory_mb)}MB"
        )
        await self._sh(cmd, timeout=600)
        try:
            await self._sh(
                f"lxc config device override {_q(name)} root size={int(disk_gb)}GB",
                timeout=120,
            )
        except PVEError:
            # Some storage drivers do not expose a resizable root device.
            # Keep the container but surface the disk problem to the caller.
            await self.delete(vmid)
            raise

        # Configure hostname + root password. cloud-init is not relied upon.
        inner = (
            f"printf '%s\\n' {_q(hostname)} > /etc/hostname; "
            f"hostname {_q(hostname)} 2>/dev/null || true; "
            f"printf '%s:%s\\n' root {_q(password)} | chpasswd; "
            "if ! command -v sshd >/dev/null 2>&1; then "
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server; "
            "fi; "
            "mkdir -p /etc/ssh/sshd_config.d; "
            "printf '%s\\n' 'PermitRootLogin yes' 'PasswordAuthentication yes' > /etc/ssh/sshd_config.d/99-root-login.conf; "
            "systemctl restart ssh 2>/dev/null || service ssh restart 2>/dev/null || true"
        )
        try:
            await self._sh(f"lxc exec {_q(name)} -- bash -lc {_q(inner)}", timeout=300)
        except PVEError:
            # Password setup failure should not leave an inaccessible VPS behind.
            await self.delete(vmid)
            raise

    async def set_root_password(self, vmid: int, password: str):
        """Set the root password inside an existing LXC container."""
        name = lxc_name(vmid)
        inner = f"printf '%s:%s\n' root {_q(password)} | chpasswd"
        await self._sh(f"lxc exec {_q(name)} -- bash -lc {_q(inner)}", timeout=60)

    async def rebuild(self, vmid, hostname, cores, memory_mb, disk_gb, password, image):
        """Destroy and recreate the same LXD VPS ID with a different OS image."""
        await self.delete(vmid)
        await self.create(vmid, hostname, cores, memory_mb, disk_gb, password, image=image)

    async def tailscale_ip(self, vmid: int) -> str | None:
        name = lxc_name(vmid)
        try:
            raw = await self._sh(f"lxc exec {_q(name)} -- bash -lc 'tailscale ip -4 2>/dev/null | head -n1'", timeout=20)
        except PVEError:
            return None
        ip = raw.strip().splitlines()[0] if raw.strip() else None
        return ip if ip and re.fullmatch(r"100\.\d+\.\d+\.\d+", ip) else None

    async def status(self, vmid) -> dict:
        res = await self._measure([int(vmid)], detail=True)
        return res[int(vmid)]

    async def list_live(self) -> dict:
        return await self._measure(None, detail=False)

    async def start(self, vmid):
        if await self._state(vmid) != "running":
            await self._sh(f"lxc start {_q(lxc_name(vmid))}", timeout=120)

    async def stop(self, vmid, force=False):
        if await self._state(vmid) == "stopped":
            return
        if force:
            await self._sh(f"lxc stop {_q(lxc_name(vmid))} --force", timeout=120)
        else:
            await self._sh(f"lxc stop {_q(lxc_name(vmid))}", timeout=120)

    async def reboot(self, vmid):
        name = lxc_name(vmid)
        if await self._state(vmid) == "running":
            await self._sh(f"lxc restart {_q(name)}", timeout=120)
        else:
            await self._sh(f"lxc start {_q(name)}", timeout=120)

    async def set_resources(self, vmid, cores=None, memory_mb=None):
        name = lxc_name(vmid)
        if cores is not None:
            await self._sh(f"lxc config set {_q(name)} limits.cpu {int(cores)}", timeout=60)
        if memory_mb is not None:
            await self._sh(f"lxc config set {_q(name)} limits.memory {int(memory_mb)}MB", timeout=60)

    async def resize_disk(self, vmid, size_gb):
        await self._sh(
            f"lxc config device override {_q(lxc_name(vmid))} root size={int(size_gb)}GB",
            timeout=120,
        )

    async def tailscale_authorize(self, vmid: int) -> tuple[str | None, str | None]:
        """Install/start Tailscale in an LXD VPS and return (login_url, current_ip)."""
        name = lxc_name(vmid)
        install = r'''set -eu
export DEBIAN_FRONTEND=noninteractive
if ! command -v tailscale >/dev/null 2>&1; then
  command -v curl >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq curl ca-certificates; }
  curl -fsSL https://tailscale.com/install.sh | sh
fi
mkdir -p /var/lib/tailscale /run/tailscale
systemctl enable --now tailscaled >/dev/null 2>&1 || true
service tailscaled start >/dev/null 2>&1 || true
if ! pgrep -x tailscaled >/dev/null 2>&1; then
  nohup tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock >/tmp/tailscaled-bot.log 2>&1 </dev/null &
  for i in $(seq 1 10); do
    [ -S /run/tailscale/tailscaled.sock ] && break
    sleep 1
  done
fi
if tailscale ip -4 2>/dev/null | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "IP|$(tailscale ip -4 | head -n1)"
  exit 0
fi
rm -f /tmp/xzy-tailscale-auth.log /tmp/xzy-tailscale-auth.pid
nohup tailscale up --hostname=__HOSTNAME__ >/tmp/xzy-tailscale-auth.log 2>&1 </dev/null &
echo $! >/tmp/xzy-tailscale-auth.pid
for i in $(seq 1 20); do
  if tailscale ip -4 2>/dev/null | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "IP|$(tailscale ip -4 | head -n1)"
    exit 0
  fi
  U=$(grep -oE 'https://login\.tailscale\.com/a/[A-Za-z0-9_-]+' /tmp/xzy-tailscale-auth.log 2>/dev/null | head -n1 || true)
  if [ -n "$U" ]; then echo "URL|$U"; exit 0; fi
  sleep 1
done
U=$(grep -oE 'https://login\.tailscale\.com/a/[A-Za-z0-9_-]+' /tmp/xzy-tailscale-auth.log 2>/dev/null | head -n1 || true)
[ -n "$U" ] && echo "URL|$U" || {
  ERR=$(tail -n 12 /tmp/xzy-tailscale-auth.log 2>/dev/null | tr "\n" " " | cut -c1-500)
  echo "E|Tailscale authorization URL nahi mila. ${ERR:-tailscaled start nahi hua}"
  exit 1
}
'''
        script = install.replace("__HOSTNAME__", f"xzy-vps-{int(vmid)}")
        prep = (
            f"lxc config set {_q(name)} security.nesting true 2>/dev/null || true; "
            f"if ! lxc config device show {_q(name)} 2>/dev/null | grep -q '^tailscale-tun:'; then "
            f"lxc config device add {_q(name)} tailscale-tun unix-char path=/dev/net/tun 2>/dev/null || true; fi; "
            f"lxc exec {_q(name)} -- bash -lc {_q(script)}"
        )
        raw = await self._sh(prep, timeout=240)
        url = None
        ip = None
        for line in raw.splitlines():
            kind, _, value = line.partition("|")
            if kind == "URL":
                url = value.strip()
            elif kind == "IP":
                ip = value.strip()
        return url, ip

    async def tailscale_ip(self, vmid: int) -> str | None:
        """Read the current Tailscale IPv4 from inside the VPS."""
        name = lxc_name(vmid)
        try:
            raw = await self._sh(
                f"lxc exec {_q(name)} -- bash -lc {_q('tailscale ip -4 2>/dev/null | head -n1')}",
                timeout=30,
            )
        except PVEError:
            return None
        ip = raw.strip().splitlines()[0] if raw.strip() else ""
        return ip if re.fullmatch(r"100\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}", ip) else None

    async def delete(self, vmid):
        name = lxc_name(vmid)
        try:
            state = await self._state(vmid)
            if state != "stopped":
                await self._sh(f"lxc stop {_q(name)} --force", timeout=120)
            await self._sh(f"lxc delete {_q(name)}", timeout=300)
        except PVEError as e:
            if "does not exist" in str(e).lower():
                return
            raise
        self._prev.pop(int(vmid), None)
