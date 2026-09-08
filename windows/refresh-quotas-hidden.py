"""Launch the quota refresh PowerShell script without creating a window."""

from __future__ import annotations

import subprocess
import sys
import shutil
from pathlib import Path


def _is_usable_executable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _find_powershell() -> Path | None:
    candidates: list[Path] = []
    pwsh_found = shutil.which("pwsh.exe")
    if pwsh_found:
        candidates.append(Path(pwsh_found))
    candidates.extend(
        [
            Path(r"C:\Program Files\PowerShell\7\pwsh.exe"),
            Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"),
        ]
    )
    for candidate in candidates:
        if _is_usable_executable(candidate):
            return candidate
    return None


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    script = root / "windows" / "refresh-quotas.ps1"
    pwsh = _find_powershell()
    if pwsh is None:
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
    for argument in sys.argv[1:]:
        if argument == "--partial":
            command.append("-Partial")
        elif argument == "--partial-red":
            command.append("-PartialRed")
        else:
            raise SystemExit(f"unsupported argument: {argument}")
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
