"""Capture a Grafana dashboard as PNG frames and assemble them into a GIF.

Uses a Chromium-based browser that is already installed (Edge or Chrome) in
headless mode, so nothing needs to be installed. The browser gets its own
throwaway profile directory inside the project's `data/` folder; the user's
real browser profile is never touched.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

BROWSER_CANDIDATES = (
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
BROWSER_NAMES = ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome")
RENDER_BUDGET_MS = 10_000
SHOT_TIMEOUT_S = 60


class CaptureError(RuntimeError):
    """A screenshot could not be taken."""


def find_browser() -> str:
    """Path of an installed Chromium-based browser."""
    for candidate in BROWSER_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    raise CaptureError("no Edge, Chrome or Chromium found; install one to capture screenshots")


def screenshot(browser: str, url: str, target: Path, profile: Path, size: tuple[int, int]) -> None:
    """Render `url` headlessly and save a PNG."""
    target.parent.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        browser,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={size[0]},{size[1]}",
        # Let the page load its data and draw before the picture is taken.
        f"--virtual-time-budget={RENDER_BUDGET_MS}",
        f"--screenshot={target}",
        url,
    ]
    try:
        subprocess.run(command, capture_output=True, timeout=SHOT_TIMEOUT_S, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptureError(f"browser failed: {exc}") from exc
    if not target.is_file() or target.stat().st_size == 0:
        raise CaptureError(f"browser produced no screenshot for {url}")


def capture_frames(
    url: str,
    directory: Path,
    profile: Path,
    *,
    frames: int,
    interval_s: float,
    size: tuple[int, int] = (1500, 950),
    on_frame: Callable[[int, Path], None] | None = None,
) -> list[Path]:
    """Take `frames` screenshots, one every `interval_s` seconds (start to start)."""
    browser = find_browser()
    paths = []
    for index in range(frames):
        started = time.monotonic()
        target = directory / f"frame-{index:03d}.png"
        screenshot(browser, url, target, profile, size)
        paths.append(target)
        if on_frame:
            on_frame(index, target)
        remaining = interval_s - (time.monotonic() - started)
        if index < frames - 1 and remaining > 0:
            time.sleep(remaining)
    return paths


def build_gif(frames: list[Path], target: Path, *, frame_ms: int = 900, width: int = 1000) -> None:
    """Assemble PNG frames into an optimised, looping GIF."""
    from PIL import Image

    images = []
    for path in frames:
        with Image.open(path) as image:
            height = round(image.height * width / image.width)
            resized = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
            images.append(resized.quantize(colors=128, method=Image.Quantize.MEDIANCUT))
    if not images:
        raise CaptureError("no frames to assemble")
    target.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        target,
        save_all=True,
        append_images=images[1:],
        duration=[frame_ms] * (len(images) - 1) + [frame_ms * 4],
        loop=0,
        optimize=True,
    )
