"""Launch the quota refresh PowerShell script without creating a window."""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    script = root / "windows" / "refresh-quotas.ps1"
    pwsh_found = shutil.which("pwsh.exe")
    pwsh = Path(pwsh_found) if pwsh_found else Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
    if not pwsh.is_file():
        return 2
    command = [
        str(pwsh),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
    ]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    completed = subprocess.run(
        command,
        cwd=root,
        startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
