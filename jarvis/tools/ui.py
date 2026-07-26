"""UI understanding: see what is clickable, click by name, type into fields.

This is what turns blind coordinate-clicking into something deliberate. Windows
exposes an accessibility tree (UI Automation) describing every control on
screen — its name, what kind of thing it is, where it sits, and how to operate
it. These tools read that tree so JARVIS can decide "click the Search button"
instead of guessing at pixels, and can tell whether an app is actually open.

Pair with see_screen (in screen.py): the vision model describes what a screen
*looks* like; this reports what is *actionable* on it and does the acting.
"""

from __future__ import annotations

import time
from typing import Literal

from jarvis.tools.registry import tool

# Control kinds worth surfacing to the model — the things you can actually act
# on. The long tail (panes, groups, separators) is noise for this purpose.
INTERACTIVE = {
    "ButtonControl", "EditControl", "HyperlinkControl", "ListItemControl",
    "MenuItemControl", "TabItemControl", "CheckBoxControl", "RadioButtonControl",
    "ComboBoxControl", "TreeItemControl", "SplitButtonControl",
}

MAX_ELEMENTS = 120
MAX_DEPTH = 12


def _root(window_title: str):
    """Resolve a window by title (fuzzy), or the foreground window if blank."""
    import uiautomation as auto

    if not window_title:
        return auto.GetForegroundControl()
    win = auto.WindowControl(searchDepth=1, SubName=window_title)
    if win.Exists(maxSearchSeconds=2):
        return win
    # Some apps (Electron, browsers) nest the real window a level down.
    win = auto.PaneControl(searchDepth=1, SubName=window_title)
    if win.Exists(maxSearchSeconds=1):
        return win
    return None


# Controls that accept typed text. DocumentControl covers modern apps (Windows
# 11 Notepad, editors, chat boxes) whose text area is not a plain EditControl.
EDITABLE = {"EditControl", "ComboBoxControl", "DocumentControl"}


def _focus_window(control) -> None:
    """Bring the control's top-level window to the foreground before typing.

    Keyboard input goes to whatever is focused, so without this a fallback
    keystroke path types into the wrong window.
    """
    import win32con
    import win32gui

    try:
        hwnd = control.NativeWindowHandle
        if not hwnd:
            top = control.GetTopLevelControl()
            hwnd = top.NativeWindowHandle if top else 0
    except Exception:  # noqa: BLE001
        hwnd = 0
    if not hwnd:
        return
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def _center(control) -> tuple[int, int] | None:
    """On-screen click point for a control, or None if it has no real rect."""
    try:
        r = control.BoundingRectangle
    except Exception:  # noqa: BLE001
        return None
    if r is None:
        return None
    w, h = r.right - r.left, r.bottom - r.top
    # Zero-size or off-screen (collapsed/hidden) controls are not clickable.
    if w <= 1 or h <= 1 or (r.left <= 0 and r.top <= 0):
        return None
    return (r.left + r.right) // 2, (r.top + r.bottom) // 2


@tool(category="ui")
def list_ui_elements(window_title: str = "",
                     kind: Literal["interactive", "text", "all"] = "interactive",
                     ) -> dict:
    """List the on-screen controls of a window and where to click them.

    Use this before clicking anything you have not been given exact coordinates
    for. It returns buttons, text boxes, links, list items and menu entries with
    their names and click points, so you can act on "the Search box" or "the
    Play button" by name instead of guessing.

    Args:
        window_title: Which window to inspect. Blank uses the focused window.
        kind: "interactive" for actionable controls only (default), "text" for
            readable labels, "all" for both.
    """
    import uiautomation as auto

    root = _root(window_title)
    if root is None:
        return {"error": f"no window matching '{window_title}'"}

    want_interactive = kind in ("interactive", "all")
    want_text = kind in ("text", "all")
    elements: list[dict] = []

    def walk(control, depth: int = 0) -> None:
        if depth > MAX_DEPTH or len(elements) >= MAX_ELEMENTS:
            return
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            if len(elements) >= MAX_ELEMENTS:
                return
            try:
                ctype = child.ControlTypeName
                name = (child.Name or "").strip()
            except Exception:  # noqa: BLE001
                continue
            is_interactive = ctype in INTERACTIVE
            if name and ((want_interactive and is_interactive)
                         or (want_text and ctype == "TextControl")):
                point = _center(child)
                if point is not None:
                    entry = {
                        "name": name[:60],
                        "type": ctype.replace("Control", "").lower(),
                        "x": point[0],
                        "y": point[1],
                    }
                    if child.ControlTypeName == "EditControl":
                        try:
                            entry["value"] = (child.GetValuePattern().Value or "")[:60]
                        except Exception:  # noqa: BLE001
                            pass
                    elements.append(entry)
            walk(child, depth + 1)

    walk(root)
    try:
        window_name = root.Name
    except Exception:  # noqa: BLE001
        window_name = window_title
    return {
        "window": window_name,
        "count": len(elements),
        "elements": elements,
        "truncated": len(elements) >= MAX_ELEMENTS,
    }


def _find_control(root, name: str, control_types: set[str] | None = None):
    """Best match for a control by name: exact, then startswith, then contains."""
    needle = name.lower().strip()
    exact = start = partial = None

    def walk(control, depth: int = 0):
        nonlocal exact, start, partial
        if depth > MAX_DEPTH or exact is not None:
            return
        try:
            children = control.GetChildren()
        except Exception:  # noqa: BLE001
            return
        for child in children:
            try:
                cname = (child.Name or "").strip().lower()
                ctype = child.ControlTypeName
            except Exception:  # noqa: BLE001
                continue
            if cname and (control_types is None or ctype in control_types):
                if _center(child) is not None:
                    if cname == needle and exact is None:
                        exact = child
                    elif cname.startswith(needle) and start is None:
                        start = child
                    elif needle in cname and partial is None:
                        partial = child
            walk(child, depth + 1)

    walk(root)
    return exact or start or partial


@tool(destructive=True, category="ui")
def click_element(name: str, window_title: str = "",
                  double: bool = False) -> dict:
    """Click a control by its name rather than by coordinates.

    Finds the button, link, list item or menu entry whose label matches and
    clicks it — the reliable way to operate an app. Run list_ui_elements first
    if you are unsure what a control is called.

    Args:
        name: The visible label of the thing to click, e.g. "Search", "Play",
            "Sign in". Matched loosely.
        window_title: Which window to look in. Blank uses the focused window.
        double: Double-click instead of single (e.g. to open a list item).
    """
    root = _root(window_title)
    if root is None:
        return {"error": f"no window matching '{window_title}'"}

    control = _find_control(root, name)
    if control is None:
        return {"error": f"no control named '{name}' found. Call list_ui_elements "
                         "to see what is available."}

    label = (control.Name or name).strip()
    point = _center(control)

    # Prefer the accessibility invoke pattern — it activates the control without
    # moving the mouse and works even when the control is scrolled partly off.
    try:
        control.SetFocus()
    except Exception:  # noqa: BLE001
        pass
    try:
        if not double:
            pattern = control.GetInvokePattern()
            if pattern:
                pattern.Invoke()
                return {"clicked": label, "via": "invoke"}
    except Exception:  # noqa: BLE001
        pass

    # Fall back to a real mouse click at the control's centre.
    if point is None:
        return {"error": f"'{label}' has no clickable position on screen"}
    import pyautogui

    pyautogui.click(x=point[0], y=point[1], clicks=2 if double else 1, interval=0.1)
    return {"clicked": label, "via": "double_click" if double else "click",
            "at": list(point)}


@tool(destructive=True, category="ui")
def type_in_field(text: str, field_name: str = "", window_title: str = "",
                  submit: bool = False) -> dict:
    """Type text into a specific text box or search field by name.

    Use this to search inside an app or fill a field: it finds the box, focuses
    it, clears it and types. This is how you "search for X" in a program.

    Args:
        text: What to type.
        field_name: Label or placeholder of the field, e.g. "Search". Blank
            uses the first editable field found, or whatever already has focus.
        window_title: Which window. Blank uses the focused window.
        submit: Press Enter afterwards, e.g. to run a search.
    """
    import pyautogui

    root = _root(window_title)
    if root is None:
        return {"error": f"no window matching '{window_title}'"}

    control = None
    if field_name:
        control = _find_control(root, field_name, EDITABLE)
    else:
        # No name given: take the first editable field in the window.
        control = _find_control(root, "", EDITABLE)

    if control is None and field_name:
        return {"error": f"no field named '{field_name}'. Call list_ui_elements "
                         "to see the fields."}

    # Bring the window forward and focus the field, so keystrokes land here.
    _focus_window(control or root)
    if control is not None:
        try:
            control.SetFocus()
        except Exception:  # noqa: BLE001
            point = _center(control)
            if point:
                pyautogui.click(x=point[0], y=point[1])
        time.sleep(0.15)
        # The value pattern is instant and reliable for plain text boxes; many
        # rich editors do not support it, so fall through to keystrokes.
        try:
            control.GetValuePattern().SetValue(text)
            if submit:
                pyautogui.press("enter")
            return {"typed": text, "into": (control.Name or field_name or "field"),
                    "via": "value_pattern", "submitted": submit}
        except Exception:  # noqa: BLE001
            pass

    # Keystroke path: select-all, replace, optionally submit.
    time.sleep(0.15)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.05)
    pyautogui.write(text, interval=0.01)
    if submit:
        pyautogui.press("enter")
    return {"typed": text, "into": field_name or "focused field",
            "via": "keyboard", "submitted": submit}


@tool(category="ui")
def is_app_running(name: str) -> dict:
    """Check whether an application is currently OPEN and running right now.

    Looks at both running processes and open windows, so it catches an app that
    is running in the tray as well as one with a visible window.

    This does NOT answer whether something is INSTALLED. An installed program
    that simply is not open right now returns running=False here, which would
    make "do I have X installed?" answer wrongly. For that question use
    find_installed_app instead.

    Args:
        name: App or process name, e.g. "steam", "chrome", "spotify".
    """
    import psutil
    import win32gui

    needle = name.lower().removesuffix(".exe")
    processes = []
    for proc in psutil.process_iter(["pid", "name"]):
        pname = (proc.info["name"] or "").lower().removesuffix(".exe")
        if needle in pname:
            processes.append({"pid": proc.info["pid"], "name": proc.info["name"]})

    windows = []

    def cb(hwnd: int, _: object) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and needle in title.lower():
                windows.append(title)
        return True

    win32gui.EnumWindows(cb, None)

    running = bool(processes)
    result = {
        "app": name,
        "running": running,
        "process_count": len(processes),
        "processes": processes[:8],
        "matching_windows": windows[:8],
    }

    # When it is not running, answer the question that was probably meant. The
    # model reliably reaches for this tool on "do I have X installed?", and a
    # bare running=False there produces a confidently wrong "no". Consulting the
    # machine map costs nothing and turns that into a correct answer whichever
    # tool was chosen — cheaper than trying to prompt the confusion away.
    if not running:
        try:
            from jarvis.core import machine_map

            matches = machine_map.find_app(name)
            if matches:
                result["installed"] = True
                result["installed_as"] = matches[0]["name"]
                result["note"] = (
                    f"Not running, but {matches[0]['name']} IS installed on this "
                    "machine. If the question was whether it is installed, the "
                    "answer is yes."
                )
            else:
                result["installed"] = False
                result["note"] = ("Not running, and nothing matching it is in the "
                                  "machine map either.")
        except Exception:  # noqa: BLE001 - the map is an enhancement, not a dependency
            pass

    return result
