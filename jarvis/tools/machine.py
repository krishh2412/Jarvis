"""Tools over the machine map: look apps up, refresh the map, ground a choice.

The map itself is built in jarvis.core.machine_map. These are the handles the
model reaches for when it needs to know what is actually installed rather than
guess — the difference between "open my editor" resolving to the right binary
and firing a shell error.
"""

from __future__ import annotations

from jarvis.core import machine_map
from jarvis.tools.registry import tool


@tool(category="system")
def find_installed_app(query: str) -> dict:
    """Look up an installed application in the machine map by spoken name.

    Use this to ground a launch before acting: "open my browser" or "spotify"
    resolves here to the app's real name and executable. If several match, they
    are returned best-first so you can pick — or ask which one if it is
    genuinely ambiguous and the choice matters.

    Args:
        query: What the user called the app ("browser", "editor", "photoshop").
    """
    matches = machine_map.find_app(query)
    if not matches:
        return {"matches": [], "note": "nothing in the machine map matches; it "
                "may not be installed, or the map may be stale - try "
                "refresh_machine_map, or launch_app which also searches the "
                "Start Menu live."}
    return {"matches": [
        {"name": m["name"], "exe": m["exe"], "aliases": m.get("aliases", [])}
        for m in matches
    ]}


@tool(category="system")
def refresh_machine_map() -> dict:
    """Rescan this PC and rebuild the machine map (apps, folders, hardware).

    Do this when something changed — a newly installed app is missing, a drive
    was added — or when the map has never been built. Takes a couple of seconds.
    """
    m = machine_map.build()
    return {
        "refreshed": m["refreshed"],
        "apps_indexed": len(m.get("apps", [])),
        "disks": len(m.get("hardware", {}).get("disks", [])),
        "project_folders": len(m.get("folders", {}).get("projects", [])),
    }


@tool(category="system")
def machine_overview() -> dict:
    """Report the machine map's summary: hardware, OS, folders, installed apps.

    A quick way to answer "what are my specs", "what's installed", "where do my
    downloads go" without rescanning. Builds the map first if it has never run.
    """
    m = machine_map.load()
    if m is None:
        m = machine_map.build()
    return {
        "hardware": m.get("hardware", {}),
        "os": m.get("os", {}),
        "folders": m.get("folders", {}),
        "app_count": len(m.get("apps", [])),
        "app_names": [a["name"] for a in m.get("apps", [])],
    }
