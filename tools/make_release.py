"""Build dist/Stanton-Trade-Optimizer-<version>.zip to send to someone.

The zip is made from the last commit with `git archive`, so it can only hold
committed files: never .env, cache/, logs/, .venv/ or config.toml (your
calibration, wallet and ships). It ships config.default.toml instead, which
the app copies to config.toml on first run. Tests and fixtures stay out.

As a last check it refuses to write a zip containing your Windows user name
or any line of the untracked file .release-guard (one string per line).

It also prints the screenshot line that goes at the top of the release notes,
pinned to this commit so the release keeps showing this version's screen, and
warns when docs/screenshot.png hasn't changed since the last release.
Commit first, then run:

    .venv\\Scripts\\python.exe tools\\make_release.py
"""
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sto import __version__  # noqa: E402

SHIP = ["sto", "config.default.toml", "requirements.txt", "run.cmd", "setup.cmd", "setup.ps1", "README.md",
        "docs/screenshot.png", "docs/API_FIELDS.md", "tools/install_tabby_profile.py"]


def guard_strings() -> list[bytes]:
    found = {Path.home().name, os.environ.get("USERNAME", ""), str(Path.home())}
    guard = ROOT / ".release-guard"
    if guard.exists():
        found |= {line.strip() for line in guard.read_text(encoding="utf-8").splitlines()}
    return [s.encode() for s in found if len(s.strip()) >= 4]


def main() -> int:
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *SHIP],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if dirty:
        print("Commit these first (the zip is built from the last commit):\n" + dirty)
        return 1
    out = ROOT / "dist" / f"Stanton-Trade-Optimizer-{__version__}.zip"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "archive", "--format=zip", "--prefix=Stanton Trade Optimizer/", "-o", str(out),
                    "HEAD", "--", *SHIP], cwd=ROOT, check=True)
    problem = check(out)
    if problem:
        out.unlink()     # after the zip is closed: Windows won't delete an open file
        print(f"Refusing: {problem}")
        return 1
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    print(f"Wrote {out} ({out.stat().st_size // 1024} KB, {len(names)} entries)")
    print()
    print("Start the release notes with this line (push first, so the image exists on GitHub):")
    print(screenshot_markdown())
    stale = screenshot_unchanged_since()
    if stale:
        print()
        print(f"Note: docs/screenshot.png is the same as in {stale}. If the screen changed, "
              "re-render it (tools/screenshot.py) and commit before releasing.")
    return 0


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def screenshot_markdown() -> str:
    remote = git("remote", "get-url", "origin").removesuffix(".git")
    repo = "/".join(remote.replace(":", "/").split("/")[-2:])
    return (f"![Stanton Trade Optimizer {__version__}](https://raw.githubusercontent.com/{repo}/"
            f"{git('rev-parse', 'HEAD')}/docs/screenshot.png)")


def screenshot_unchanged_since() -> str | None:
    """The last release tag, when the screenshot hasn't changed since it."""
    tag = git("describe", "--tags", "--abbrev=0")
    if not tag:
        return None
    changed = subprocess.run(["git", "diff", "--quiet", tag, "HEAD", "--", "docs/screenshot.png"], cwd=ROOT).returncode
    return tag if changed == 0 else None


def check(out: Path) -> str | None:
    guards = guard_strings()
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        if any(n.endswith("/config.toml") for n in names):
            return "the zip would contain a personal config.toml"
        for name in names:
            data = z.read(name)
            hits = [g.decode() for g in guards if g.lower() in data.lower()]
            if hits and not name.endswith(".png"):
                return f"{name} contains {hits}"
        defaults = z.read("Stanton Trade Optimizer/config.default.toml").decode()
        if "region = [0, 0, 0, 0]" not in defaults or "balance = 0" not in defaults:
            return "config.default.toml isn't clean"
    return None


if __name__ == "__main__":
    sys.exit(main())
