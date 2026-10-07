"""A stand-in for the MDA sandbox backend that runs commands on this machine.

Only for tests of the sandbox blueprint's tools. `execute` runs the command in a
shell whose `link-cli` is a real CLI binary pointed at fake Link servers, and
whose filesystem paths under the sandbox's auth directory map into a temp folder.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace


class LocalSandboxBackend:
    def __init__(self, cli: Path, *, api_url: str, auth_url: str, auth_dir: str):
        self.root = Path(tempfile.mkdtemp(prefix="local-sandbox-"))
        self.auth_dir = auth_dir
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "link-cli"
        shim.write_text(f'#!/bin/sh\nexec node {shlex.quote(str(cli))} "$@"\n')
        shim.chmod(0o755)
        home = self.root / "home"
        home.mkdir()
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(home),
            "LINK_API_BASE_URL": api_url,
            "LINK_AUTH_BASE_URL": auth_url,
            "NO_UPDATE_NOTIFIER": "1",
        }
        self.commands: list[str] = []

    def _map(self, path: str) -> Path:
        assert path.startswith(self.auth_dir + "/") or path == self.auth_dir, path
        return self.root / "fs" / path.lstrip("/")

    def _rewrite(self, command: str) -> str:
        return command.replace(self.auth_dir, str(self.root / "fs" / self.auth_dir.lstrip("/")))

    async def aexecute(self, command: str, *, timeout: int | None = None):
        self.commands.append(command)
        result = await asyncio.to_thread(
            subprocess.run,
            ["bash", "-c", self._rewrite(command)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout or 120,
        )
        return SimpleNamespace(
            output=result.stdout + result.stderr, exit_code=result.returncode, truncated=False
        )

    async def awrite(self, file_path: str, content: str):
        target = self._map(file_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return SimpleNamespace(error=None, path=file_path)

    async def adelete(self, file_path: str):
        target = self._map(file_path)
        existed = target.exists()
        if existed:
            target.unlink()
        return SimpleNamespace(error=None if existed else "file_not_found", path=file_path)

    async def adownload_files(self, paths):
        return [SimpleNamespace(error=None, content=self._map(path).read_bytes()) for path in paths]

    def files(self) -> list[str]:
        base = self.root / "fs"
        return (
            sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())
            if base.exists()
            else []
        )
