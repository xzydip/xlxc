import os
from dotenv import load_dotenv

load_dotenv()


def _req(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int_list(key: str) -> list[int]:
    return [int(x) for x in os.getenv(key, "").replace(" ", "").split(",") if x]


def _first(*keys: str, default: str = "") -> str:
    """First non-empty env var among keys (lets old MINING_* names keep working)."""
    for k in keys:
        v = os.getenv(k)
        if v:
            return v
    return default


class Config:
    token = _req("DISCORD_TOKEN")
    # Main owner: full admin + the only one who can /addadmin and /removeadmin
    owner_id = int(os.getenv("OWNER_ID", "0") or 0)
    guild_id = int(os.getenv("GUILD_ID") or 0)

    # ---- How the bot reaches the LXD host (lxc CLI) ----
    exec_mode = _first("HOST_EXEC_MODE", "MINING_EXEC_MODE", default="local").lower()   # local | ssh
    ssh_host = _first("HOST_SSH_HOST", "MINING_SSH_HOST")
    ssh_port = int(_first("HOST_SSH_PORT", "MINING_SSH_PORT", default="22"))
    ssh_user = _first("HOST_SSH_USER", "MINING_SSH_USER", default="root")
    ssh_key = _first("HOST_SSH_KEY", "MINING_SSH_KEY")
    ssh_password = _first("HOST_SSH_PASSWORD", "MINING_SSH_PASSWORD")
    if exec_mode == "ssh" and not ssh_host:
        raise RuntimeError("HOST_EXEC_MODE=ssh hai to HOST_SSH_HOST required hai")

    # LXD image alias, e.g. images:ubuntu/22.04 or ubuntu:22.04
    image = _first("LXC_IMAGE", "LXC_TEMPLATE", default="images:ubuntu/22.04")
    # LXD storage pool name (shown by: lxc storage list)
    storage = os.getenv("LXC_STORAGE", "")
    vmid_start = int(os.getenv("VMID_START", 1000))

    # ---- Tailscale ----
    # API token is used only to verify/find the VPS's Tailscale 100.x address.
    tailscale_api_key = os.getenv("TAILSCALE_API_KEY", "").strip()

    timezone = os.getenv("TIMEZONE", "Asia/Kolkata")
    check_interval_min = int(os.getenv("CHECK_INTERVAL_MIN", 5))
    warn_hours = int(os.getenv("WARN_HOURS", 24))
    grace_days = int(os.getenv("GRACE_DAYS", 3))

    db_path = os.getenv("DB_PATH", "vps.db")

    # ---- Free VPS (/deploy). Admin can override specs/on-off with /deployspecs, /deployvps ----
    deploy_default_enabled = _bool("DEPLOY_ENABLED", True)
    free_ram_mb = int(os.getenv("FREE_RAM_MB", 1024))
    free_cpu = int(os.getenv("FREE_CPU", 1))
    free_disk_gb = int(os.getenv("FREE_DISK_GB", 10))
    free_expiry = os.getenv("FREE_EXPIRY", "30d")
    free_max_total = int(os.getenv("FREE_MAX_TOTAL", 0))       # 0 = unlimited
    free_per_user_limit = int(os.getenv("FREE_PER_USER_LIMIT", 1)) # 0 = unlimited per user

    # ---- Mining protection ----
    mining_enabled = _bool("MINING_PROTECTION", True)
    mining_action = os.getenv("MINING_ACTION", "delete").lower()          # delete | alert
    mining_scan_interval = int(os.getenv("MINING_SCAN_INTERVAL_SEC", 60))
    mining_delete_score = int(os.getenv("MINING_DELETE_SCORE", 100))
    mining_alert_score = int(os.getenv("MINING_ALERT_SCORE", 50))
    mining_cpu_threshold = float(os.getenv("MINING_CPU_THRESHOLD", 0.90))  # 0-1 of allocated vCPU
    mining_cpu_window = int(os.getenv("MINING_CPU_WINDOW", 10))            # consecutive scans
    mining_pool_ports = set(_int_list("MINING_POOL_PORTS")) or {
        3333, 3334, 4444, 5555, 7777, 14433, 14444, 45560, 45700}
    mining_exempt = set(_int_list("MINING_EXEMPT_VMIDS"))


cfg = Config()
