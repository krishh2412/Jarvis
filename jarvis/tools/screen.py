"""Screen capture and input synthesis — eyes and hands."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from jarvis.config import DATA_DIR
from jarvis.tools.registry import tool

SHOTS_DIR = DATA_DIR / "screenshots"
SHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Keep the last N captures; screenshots accumulate fast at 2560x1440.
MAX_SHOTS = 40


def _prune_shots() -> None:
    shots = sorted(SHOTS_DIR.glob("shot-*.png"), key=lambda p: p.stat().st_mtime)
    for stale in shots[:-MAX_SHOTS]:
        stale.unlink(missing_ok=True)


@tool(category="screen")
def take_screenshot(monitor: int = 0, region: str = "") -> dict:
    """Capture the screen to a PNG file and return its path.

    Args:
        monitor: Which monitor to capture. 0 captures all of them combined,
            1 is the primary, 2 the second, and so on.
        region: Optional "left,top,width,height" pixel rectangle to crop to.
            Overrides the monitor selection.
    """
    import mss

    with mss.mss() as sct:
        if region:
            try:
                left, top, width, height = (int(v) for v in region.split(","))
            except ValueError:
                return {"error": 'region must be "left,top,width,height"'}
            box = {"left": left, "top": top, "width": width, "height": height}
        else:
            if monitor >= len(sct.monitors):
                return {
                    "error": f"monitor {monitor} not found; "
                             f"{len(sct.monitors) - 1} attached"
                }
            box = sct.monitors[monitor]

        raw = sct.grab(box)
        stamp = datetime.now().strftime("%H%M%S-%f")[:-3]
        path = SHOTS_DIR / f"shot-{stamp}.png"
        mss.tools.to_png(raw.rgb, raw.size, output=str(path))

    _prune_shots()
    return {"path": str(path), "width": raw.width, "height": raw.height}


@tool(category="screen")
def see_screen(question: str = "What is currently on the screen?",
               monitor: int = 0) -> dict:
    """Actually look at the screen and answer a question about what is there.

    Use this whenever you need to SEE, not just capture — to read what is on a
    window, describe an image, tell what app is open, find a button, check an
    error message, or answer "what am I looking at". Unlike take_screenshot,
    which only saves a file, this passes the screen to a vision model and
    returns what it sees in words.

    Args:
        question: What you want to know about the screen. Be specific — "what
            error is shown", "what's the title of the video", "is there a
            submit button and where".
        monitor: Which monitor to look at. 0 is all of them, 1 the primary.
    """
    import base64
    import io

    import mss
    from PIL import Image

    from jarvis.config import config

    # Capture.
    with mss.mss() as sct:
        if monitor >= len(sct.monitors):
            monitor = 1 if len(sct.monitors) > 1 else 0
        raw = sct.grab(sct.monitors[monitor])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    # Downscale wide frames — a 4K grab is slow to encode and to reason over,
    # and adds nothing over ~1600px for reading a screen.
    if img.width > config.vision.max_width:
        ratio = config.vision.max_width / img.width
        img = img.resize((config.vision.max_width, int(img.height * ratio)),
                         Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode()

    # Ask the vision model. Kept separate from the main brain because qwen3 is
    # text-only; this is a distinct model loaded on demand. The instruction
    # keeps the description tight — it is raw material for JARVIS to condense
    # into one spoken sentence, not the reply itself.
    import ollama

    prompt = (
        f"{question}\n\nAnswer in at most two short sentences. State only what "
        "is actually visible; do not speculate or add preamble."
    )
    try:
        client = ollama.Client(host=config.llm.host)
        response = client.chat(
            model=config.vision.model,
            messages=[{"role": "user", "content": prompt, "images": [encoded]}],
            keep_alive=config.vision.keep_alive,
            options={"temperature": 0.2},
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not found" in msg or "no such model" in msg:
            return {
                "error": f"The vision model {config.vision.model} is not "
                         f"installed. Run: ollama pull {config.vision.model}"
            }
        return {"error": f"could not look at the screen: {exc}"}

    answer = (response.get("message", {}) or {}).get("content", "").strip()
    return {"question": question, "sees": answer, "resolution": img.size}


@tool(category="screen")
def screen_info() -> dict:
    """Report monitor count and the resolution of each."""
    import mss

    with mss.mss() as sct:
        monitors = []
        for index, mon in enumerate(sct.monitors[1:], start=1):
            monitors.append(
                {
                    "index": index,
                    "width": mon["width"],
                    "height": mon["height"],
                    "left": mon["left"],
                    "top": mon["top"],
                }
            )
    return {"count": len(monitors), "monitors": monitors}


@tool(destructive=True, category="screen")
def click_at(x: int, y: int,
             button: Literal["left", "right", "middle"] = "left",
             clicks: int = 1) -> dict:
    """Click the mouse at absolute screen coordinates.

    Args:
        x: Horizontal pixel position.
        y: Vertical pixel position.
        button: Which mouse button to use.
        clicks: Number of clicks; 2 for a double-click.
    """
    import pyautogui

    pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.08)
    return {"clicked": [x, y], "button": button, "clicks": clicks}


@tool(destructive=True, category="screen")
def type_text(text: str, interval: float = 0.01) -> dict:
    """Type text into whatever currently has keyboard focus.

    Args:
        text: The text to type.
        interval: Delay between keystrokes in seconds. Raise it if the target
            application drops characters.
    """
    import pyautogui

    pyautogui.write(text, interval=interval)
    return {"typed_chars": len(text)}


@tool(destructive=True, category="screen")
def press_keys(keys: str) -> dict:
    """Press a key or a keyboard shortcut.

    Args:
        keys: A single key ("enter", "esc", "f5") or a combination joined by
            "+" such as "ctrl+s", "alt+tab" or "win+d".
    """
    import pyautogui

    parts = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not parts:
        return {"error": "no keys given"}

    if len(parts) == 1:
        pyautogui.press(parts[0])
    else:
        pyautogui.hotkey(*parts)
    return {"pressed": keys}


@tool(destructive=True, category="screen")
def scroll_screen(amount: int, x: int = -1, y: int = -1) -> dict:
    """Scroll the mouse wheel.

    Args:
        amount: Notches to scroll. Positive scrolls up, negative down.
        x: Optional cursor X to scroll at. -1 keeps the current position.
        y: Optional cursor Y to scroll at. -1 keeps the current position.
    """
    import pyautogui

    if x >= 0 and y >= 0:
        pyautogui.moveTo(x, y)
        time.sleep(0.05)
    pyautogui.scroll(amount * 120)
    return {"scrolled": amount}


@tool(destructive=True, category="screen")
def drag_mouse(from_x: int, from_y: int, to_x: int, to_y: int,
               duration: float = 0.4) -> dict:
    """Drag the mouse from one point to another with the left button held.

    Args:
        from_x: Starting X coordinate.
        from_y: Starting Y coordinate.
        to_x: Ending X coordinate.
        to_y: Ending Y coordinate.
        duration: Seconds the drag should take. Too fast and some apps miss it.
    """
    import pyautogui

    pyautogui.moveTo(from_x, from_y)
    pyautogui.dragTo(to_x, to_y, duration=duration, button="left")
    return {"dragged": [from_x, from_y], "to": [to_x, to_y]}


@tool(category="screen")
def mouse_position() -> dict:
    """Get the current mouse cursor position."""
    import pyautogui

    pos = pyautogui.position()
    return {"x": pos.x, "y": pos.y}


@tool(category="screen")
def read_window_text(title: str = "", max_items: int = 120) -> dict:
    """Read the text content of a window via the accessibility tree.

    More reliable than OCR for reading what is on screen, since it pulls
    actual control text rather than guessing at pixels. Works with most
    native and Electron applications.

    Args:
        title: Window title to read, or any distinctive part of it. Empty
            reads the currently focused window.
        max_items: Cap on the number of text elements returned.
    """
    import uiautomation as auto

    try:
        window = (
            auto.WindowControl(searchDepth=1, SubName=title)
            if title
            else auto.GetForegroundControl()
        )
        if not window.Exists(maxSearchSeconds=2):
            return {"error": f"no window matching '{title}'"}

        texts: list[str] = []

        def walk(control, depth: int = 0) -> None:
            if len(texts) >= max_items or depth > 12:
                return
            try:
                name = (control.Name or "").strip()
                if name and name not in texts:
                    texts.append(name)
                for child in control.GetChildren():
                    walk(child, depth + 1)
            except (OSError, AttributeError):
                return

        walk(window)
        return {"window": window.Name, "text_elements": texts[:max_items]}
    except Exception as exc:  # noqa: BLE001 - UIA throws a wide variety
        return {"error": f"could not read window: {exc}"}
