"""Host/LXD-side crypto-miner detection for LXD containers.

Nothing is installed inside the container. The scan runs on the Proxmox host (locally or via SSH):
  • reads the container's processes from its cgroup (/proc/<pid>/comm + cmdline)
  • reads its established TCP connections via `nsenter -t <init> -n ss`
  • tracks sustained CPU usage from the container's cgroup (cpu.stat)

Every signal adds to a score:
  100  known miner binary name / stratum URL / mining flags / pool domain / Monero wallet
   40  outbound connection to a typical mining-pool port (3333, 4444, ...)
   35  CPU pinned above threshold for the whole window
Score >= MINING_DELETE_SCORE  -> confirmed (auto action)
Score >= MINING_ALERT_SCORE   -> suspicious (log only)
So CPU-heavy-but-legit workloads (compiling, rendering) are never deleted on CPU alone.
"""
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from config import cfg
from proxmox_client import lxc_name
from host import HostError as DetectorError, HostShell  # noqa: F401  (re-exported)


NAME_HINTS = (
    "xmrig", "xmr-stak", "minerd", "cpuminer", "ethminer", "nbminer", "t-rex", "phoenixminer",
    "lolminer", "gminer", "cgminer", "bfgminer", "ccminer", "nanominer", "srbminer",
    "teamredminer", "kawpowminer", "nheqminer", "wildrig", "sgminer", "hellminer",
    "minergate", "nicehash", "cryptonight", "randomx",
)
# Tools that merely *mention* mining strings (grep stratum, nano xmrig.conf ...)
IGNORE_COMMS = {"grep", "egrep", "fgrep", "rg", "less", "more", "cat", "vim", "vi", "nano",
                "tail", "head", "man", "awk", "sed", "ssh", "scp", "git", "apt", "apt-get", "dpkg"}

ARG_RE = re.compile(
    r"stratum\+(?:tcp|ssl|tls|ws)://|--donate-level|--cpu-priority[ =]?\d|"
    r"(?:--algo|-a)[ =](?:rx/0|randomx|cryptonight|cn/|kawpow|ethash|etchash|autolykos|yescrypt|x25x)|"
    r"(?:moneroocean|minexmr|supportxmr|nanopool|2miners|f2pool|hashvault|ethermine|unmineable|"
    r"nicehash|herominers|c3pool|miningpoolhub|minergate)\.",
    re.I,
)
WALLET_RE = re.compile(r"\b4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}\b")   # Monero address

SCAN_SCRIPT = r"""
# Run entirely inside the LXD container. No Proxmox cgroup paths are required.
if ! command -v ps >/dev/null 2>&1; then echo "E|ps is missing inside container"; exit 0; fi
for p in $(ps -e -o pid= | awk '{print $1}'); do
  c=$(cat /proc/$p/comm 2>/dev/null)
  a=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
  echo "P|$p|$c|$a"
done
if command -v ss >/dev/null 2>&1; then
  ss -tnH state established 2>/dev/null | awk '{print "N|"$5}'
fi
exit 0
"""


@dataclass
class Finding:
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, pts: int, why: str):
        self.score += pts
        if why not in self.reasons:
            self.reasons.append(why)


def _short(text: str, n: int = 110) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def analyze(raw: str, cpu_samples=()) -> Finding:
    f = Finding()
    pool_hits: list[str] = []
    saw_process = False

    for line in raw.splitlines():
        kind, _, rest = line.partition("|")
        if kind == "E":
            raise DetectorError(rest or "scan failed")

        if kind == "P":
            saw_process = True
            pid, comm, args = (rest.split("|", 2) + ["", ""])[:3]
            comm, args = comm.strip().lower(), args.strip()
            if comm in IGNORE_COMMS:
                continue
            argv0 = os.path.basename(args.split()[0]).lower() if args else ""
            name = next((n for n in NAME_HINTS if n in comm or n in argv0), None)
            if name:
                f.add(100, f"Miner process `{comm or argv0}` (pid {pid})")
            elif ARG_RE.search(args):
                f.add(100, f"Mining pool/flags in command: `{_short(args)}`")
            elif WALLET_RE.search(args):
                f.add(100, f"Monero wallet in command: `{_short(args)}`")

        elif kind == "N":
            peer = rest.strip()
            port = peer.rsplit(":", 1)[-1]
            if port.isdigit() and int(port) in cfg.mining_pool_ports:
                pool_hits.append(peer)

    if not saw_process:
        raise DetectorError("No processes read from container cgroup")
    if pool_hits:
        f.add(40, "Connection to mining-pool port: " + ", ".join(sorted(set(pool_hits))[:3]))
    if (len(cpu_samples) >= cfg.mining_cpu_window
            and min(cpu_samples) >= cfg.mining_cpu_threshold):
        mins = cfg.mining_cpu_window * cfg.mining_scan_interval / 60
        f.add(35, f"CPU ≥ {cfg.mining_cpu_threshold * 100:.0f}% for ~{mins:.0f} min")
    return f


class MinerDetector:
    def __init__(self, pve):
        self.pve = pve
        self.shell = pve.shell
        self._cpu: dict[int, deque] = defaultdict(lambda: deque(maxlen=cfg.mining_cpu_window))

    async def scan(self, vmid: int, live: dict | None = None, record_cpu: bool = True) -> Finding:
        live = live or await self.pve.status(vmid)
        if record_cpu:
            self._cpu[vmid].append(live["cpu"])
        raw = await self.shell.run(f"lxc exec {lxc_name(vmid)} -- bash -lc {__import__('shlex').quote(SCAN_SCRIPT)}")
        return analyze(raw, self._cpu[vmid])

    def forget(self, vmid: int):
        self._cpu.pop(vmid, None)
