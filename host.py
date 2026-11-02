"""Runs bash scripts on the LXD host — locally, or over SSH.

This is the only way the bot talks to LXD (`lxc` CLI). No Proxmox API is required.
"""
import asyncio
import threading

from config import cfg

# ssh non-login shells sometimes miss LXD locations
_PATH_FIX = "export PATH=\"$PATH:/usr/sbin:/sbin:/usr/local/bin:/usr/bin:/bin:/snap/bin\"\n"


class HostError(Exception):
    pass


class HostShell:
    def __init__(self):
        self._client = None
        self._connect_lock = threading.Lock()

    # ---------- public ----------
    async def exec(self, script: str, timeout: int = 30) -> tuple[int, str, str]:
        """Returns (exit_code, stdout, stderr)."""
        script = _PATH_FIX + script
        if cfg.exec_mode == "ssh":
            return await asyncio.to_thread(self._ssh_exec, script, timeout)
        return await self._local_exec(script, timeout)

    async def run(self, script: str, timeout: int = 30) -> str:
        """stdout only (exit code ignored) — used by scripts that report errors via `E|...` lines."""
        _rc, out, _err = await self.exec(script, timeout)
        return out

    # ---------- local ----------
    async def _local_exec(self, script: str, timeout: int):
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", script,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise HostError("Host command timed out")
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    # ---------- ssh ----------
    def _connect(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(cfg.ssh_host, port=cfg.ssh_port, username=cfg.ssh_user,
                  key_filename=cfg.ssh_key or None, password=cfg.ssh_password or None, timeout=15)
        self._client = c

    def _ensure_connected(self):
        with self._connect_lock:
            t = self._client.get_transport() if self._client else None
            if t and t.is_active():
                return
            self._client = None
            try:
                self._connect()
            except Exception as e:
                self._client = None
                raise HostError(f"SSH connect error: {e}")

    def _ssh_exec(self, script: str, timeout: int):
        # Connect (with one reconnect) first; the command itself runs ONCE and is never
        # retried, so a slow `lxc launch` can't accidentally be executed twice.
        self._ensure_connected()
        try:
            stdin, stdout, stderr = self._client.exec_command("bash -s", timeout=timeout)
            stdin.write(script)
            stdin.channel.shutdown_write()
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            rc = stdout.channel.recv_exit_status()
            return rc, out, err
        except Exception as e:
            self._client = None
            raise HostError(f"SSH error: {e}")
