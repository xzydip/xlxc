"""Small Tailscale API client used to verify a provisioned LXD VPS node."""
import base64
import json
import urllib.error
import urllib.request

from config import cfg


class TailscaleAPIError(Exception):
    pass


def _get_devices():
    if not cfg.tailscale_api_key:
        raise TailscaleAPIError("TAILSCALE_API_KEY is not configured")
    # Use Tailscale's default/current tailnet; no TAILSCALE_TAILNET setting is required.
    url = "https://api.tailscale.com/api/v2/tailnet/-/devices"
    req = urllib.request.Request(url, method="GET")
    token = base64.b64encode((cfg.tailscale_api_key + ":").encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise TailscaleAPIError(f"HTTP {e.code}: {body}") from e
    except Exception as e:
        raise TailscaleAPIError(str(e)) from e


def find_ipv4(hostname: str) -> str | None:
    """Return the first 100.x IPv4 address for an exact machine hostname."""
    data = _get_devices()
    for dev in data.get("devices", []):
        names = {str(dev.get("hostname", "")).lower(), str(dev.get("name", "")).lower()}
        if hostname.lower() not in names:
            continue
        for addr in dev.get("addresses", []) or []:
            addr = str(addr).split("/", 1)[0]
            if addr.startswith("100."):
                return addr
    return None
