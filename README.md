# LXD/LXC Discord VPS Bot

This version is converted from the old Proxmox `pct` backend to **LXD `lxc`**.
Each VPS created by `/deploy` or `/create` is an LXD container.

## Host requirements

Install/verify LXD:
```bash
lxc version
lxc list
```

The bot must run on the LXD host as root, or connect to that host over SSH.

## Setup

```bash
python3 -m venv myenv
source myenv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set at least:
```env
DISCORD_TOKEN=...
ADMIN_IDS=...
LXC_IMAGE=ubuntu:22.04
LXC_STORAGE=
HOST_EXEC_MODE=local
```

Check your storage pool:
```bash
lxc storage list
```
If you want a specific pool, set `LXC_STORAGE` to its name.

Test image launch manually before starting the bot:
```bash
lxc launch ubuntu:22.04 vps-9999
lxc list
lxc exec vps-9999 -- bash
lxc delete vps-9999 --force
```

Start the bot:
```bash
source myenv/bin/activate
python3 bot.py
```

## Commands

| Command | Who | Function |
|---|---|---|
| `/deploy` | Everyone | Free LXD VPS |
| `/deployspecs` | Admin | Free VPS RAM/CPU/disk/expiry |
| `/deployvps true/false` | Admin | Enable/disable free deploy |
| `/setdeploylimit <n>` | Admin | Per-user `/deploy` limit; 0 = unlimited |
| `/create` | Admin | Create an LXD VPS |
| `/editvps` | Admin | Edit resources/expiry/owner |
| `/renew` | Admin | Renew/unsuspend |
| `/deletevps` | Admin | Delete VPS |
| `/listvps` | Admin | List VPS |
| `/load` | Admin | Resource load |
| `/manage` | Owner/shared/admin | Start/stop/restart/SSH/SSHX |
| `/share` | Owner/admin | Share VPS |
| `/myvps` | Everyone | Own VPS list |
| `/setlog` | Admin | Mining log channel |
| `/scan` | Admin | Mining dry-run scan |

## Important

This bot no longer needs Proxmox VE or `pct`. Do **not** install Proxmox just for this bot.
The VPS IDs are LXD container names (`1000`, `1001`, ...), so the existing Discord/database commands can keep using the same IDs.
