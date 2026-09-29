"""Chart Lab intake — get an image in without typing a path.

You cannot drag a file into this terminal, so the bench must meet the
chart where it already is. Three routes, all macOS-native and
dependency-free:

- **clipboard** — ⌃⇧⌘4 screenshots straight to the clipboard, and any
  image copied from a browser, Preview or TradingView lands there too.
  Read via `osascript` (`«class PNGf»`), which ships with the OS.
- **screenshot** — ⇧⌘4 writes a file to the screenshot folder (Desktop
  unless `com.apple.screencapture location` says otherwise). Take the
  newest one.
- **inbox** — a plain folder you can drag into from Finder. Everything
  in it is fair game, and files are moved out once parked so the same
  chart is never read twice.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from macro_positioning.core.settings import settings

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# Where a dragged chart lands. Inside the repo so Finder can reach it in
# two clicks; gitignored, because charts are data, not source.
INBOX_DIRNAME = "chart-inbox"

# Files already parked are moved here rather than deleted — a chart is
# evidence, and re-reading one should be a choice, not an accident.
INBOX_DONE_DIRNAME = "parked"


def inbox_dir() -> Path:
    path = Path(settings.base_dir) / INBOX_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def inbox_done_dir() -> Path:
    path = inbox_dir() / INBOX_DONE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def screenshot_dir() -> Path:
    """The folder ⇧⌘4 writes to — Desktop unless reconfigured."""
    try:
        raw = subprocess.run(
            ["defaults", "read", "com.apple.screencapture", "location"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if raw.returncode == 0 and raw.stdout.strip():
            return Path(raw.stdout.strip()).expanduser()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / "Desktop"


def _is_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in IMAGE_EXTS


def grab_clipboard() -> Path | None:
    """Write the clipboard's image to a temp PNG, or None if it holds none.

    `osascript` refuses with -1700 ("can't make some data into the
    expected type") when the clipboard holds text rather than an image.
    That is the normal negative case, not an error worth raising.
    """
    target = Path(tempfile.gettempdir()) / (
        f"chartlab-clip-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.png"
    )
    script = [
        "osascript",
        "-e", "set png to (the clipboard as «class PNGf»)",
        "-e", f'set f to open for access POSIX file "{target}" with write permission',
        "-e", "set eof f to 0",
        "-e", "write png to f",
        "-e", "close access f",
    ]
    try:
        proc = subprocess.run(script, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        return None
    return target


def newest_screenshot(*, within_minutes: int | None = None) -> Path | None:
    """The most recently modified image in the screenshot folder."""
    folder = screenshot_dir()
    if not folder.is_dir():
        return None
    images = [p for p in folder.iterdir() if _is_image(p)]
    if not images:
        return None
    newest = max(images, key=lambda p: p.stat().st_mtime)
    if within_minutes is not None:
        age_s = datetime.now().timestamp() - newest.stat().st_mtime
        if age_s > within_minutes * 60:
            return None
    return newest


def age_minutes(path: Path) -> float:
    """Minutes since the file was last written — shown before parking it.

    The newest screenshot on the Desktop is not necessarily the chart you
    just took, so the age is quoted rather than assumed.
    """
    return max(0.0, (datetime.now().timestamp() - path.stat().st_mtime) / 60.0)


def inbox_images() -> list[Path]:
    """Unparked images sitting in the drop folder, oldest first."""
    folder = inbox_dir()
    images = [p for p in folder.iterdir() if _is_image(p)]
    return sorted(images, key=lambda p: p.stat().st_mtime)


def mark_parked(path: Path) -> Path | None:
    """Move a consumed inbox file aside so it is not read twice."""
    if path.parent.resolve() != inbox_dir().resolve():
        return None
    target = inbox_done_dir() / path.name
    if target.exists():
        stamp = datetime.now(UTC).strftime("%H%M%S")
        target = inbox_done_dir() / f"{path.stem}-{stamp}{path.suffix}"
    try:
        shutil.move(str(path), str(target))
    except OSError:
        return None
    return target


def describe_sources() -> list[str]:
    """One line per route, for when nothing was found anywhere."""
    return [
        "⌃⇧⌘4  screenshot straight to the clipboard, then: chart grab",
        "⇧⌘4   screenshot to a file, then: chart grab --from screenshot",
        f"drag any image into {inbox_dir()}, then: chart grab --from inbox",
    ]
