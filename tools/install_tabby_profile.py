"""Add a "Trade Terminal" profile to Tabby's config.yaml (backed up first).

Usage: python tools/install_tabby_profile.py   (close Tabby first, or restart it after)
Field names follow Tabby 1.0's profile defaults (tabby-core profileDefaults,
tabby-local options).
"""
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ["APPDATA"]) / "tabby" / "config.yaml"
NAME = "Trade Terminal"
COLORS = ["#0a0a0a", "#ff4f3a", "#7ddc6a", "#ffb35c", "#5da9f6", "#e86aff", "#82fff7", "#dedacf",
          "#3a3a3a", "#ff7a66", "#a6f08f", "#ffd23f", "#a5c7ff", "#ddaaff", "#b7fff9", "#ffffff"]


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def profile_yaml() -> str:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    lines = [
        "  - type: local",
        f"    name: {q(NAME)}",
        "    icon: fas fa-chart-line",
        "    color: '#ff8a1f'",
        f"    id: {q('local:custom:sto-trade-terminal:' + str(uuid.uuid4()))}",
        "    disableDynamicTitle: true",
        "    behaviorOnSessionEnd: auto",
        "    options:",
        f"      command: {q(str(python))}",
        "      args:",
        "        - '-m'",
        "        - sto",
        f"      cwd: {q(str(ROOT))}",
        "      env:",
        "        PYTHONUTF8: '1'",
        "    terminalColorScheme:",
        "      name: STO Phosphor",
        "      foreground: '#f2ede4'",
        "      background: '#0a0a0a'",
        "      cursor: '#ff8a1f'",
        "      colors:",
        *[f"        - '{c}'" for c in COLORS],
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    if not CONFIG.exists():
        print(f"Tabby config not found at {CONFIG}. Open Tabby once, then rerun.")
        return 1
    text = CONFIG.read_text(encoding="utf-8")
    if f"name: {q(NAME)}" in text:
        print("Tabby already has a Trade Terminal profile; nothing changed.")
        return 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("profiles:"):
            break
    else:
        print("No top-level 'profiles:' key in Tabby's config; add the profile by hand (see README).")
        return 1
    backup = CONFIG.with_name(f"config.backup-{time.strftime('%Y-%m-%d-%H%M%S')}.yaml")
    shutil.copy2(CONFIG, backup)
    if line.strip() == "profiles: []":
        lines[i] = "profiles:\n" + profile_yaml()
    else:
        lines.insert(i + 1, profile_yaml())
    CONFIG.write_text("".join(lines), encoding="utf-8")
    print(f"Added the '{NAME}' profile. Backup: {backup}")
    print("Restart Tabby, then pick it from the profile list (Ctrl-Shift-E) or the + menu.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
