#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import glob
import os
import re
import shlex
import shutil
import subprocess


def is_wayland() -> bool:
    """Return True if the current session is running under Wayland."""
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


def query_xrandr() -> str:
    """Query xrandr for connected outputs (X11)."""
    query = "xrandr --query"
    xrandr_output = subprocess.Popen(
        shlex.split(query),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdout, _ = xrandr_output.communicate()
    return str(stdout, "utf-8")


def extract_displays(output: str) -> list:
    """Extract connected display names from xrandr output."""
    pattern = re.compile(r"\b({0})\b".format("connected"), flags=re.IGNORECASE)
    lines = output.splitlines()
    connected = [line for line in lines if pattern.search(line)]
    return list(map(lambda display: display.split()[0], connected))


def detect_wayland_drm_displays() -> list:
    """Detect connected displays via Linux kernel DRM sysfs under Wayland."""
    displays = []
    for status_path in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        try:
            with open(status_path, "r", encoding="utf-8") as f:
                status = f.read().strip()
            if status == "connected":
                folder_name = os.path.basename(os.path.dirname(status_path))
                # Strip 'card0-' or 'card1-' prefix to get e.g. 'eDP-1', 'HDMI-A-1'
                display_name = re.sub(r"^card\d+-", "", folder_name)
                if not display_name.startswith("Writeback") and display_name not in displays:
                    displays.append(display_name)
        except OSError:
            continue
    return displays


def detect_wlr_displays() -> list:
    """Detect connected displays via wlr-randr on wlroots compositors."""
    if not shutil.which("wlr-randr"):
        return []
    try:
        out = subprocess.check_output(["wlr-randr"], text=True, stderr=subprocess.DEVNULL)
        displays = []
        for line in out.splitlines():
            if line and not line[0].isspace():
                name = line.split()[0]
                displays.append(name)
        return displays
    except subprocess.SubprocessError:
        return []


def detect_display_devices() -> list:
    """Detect all active connected display devices on both Wayland and X11."""
    if is_wayland():
        displays = detect_wlr_displays() or detect_wayland_drm_displays()
        if displays:
            return displays

    try:
        displays = extract_displays(query_xrandr())
        # Filter out unusable XWAYLAND virtual outputs if DRM sysfs is available
        if any(d.startswith("XWAYLAND") for d in displays):
            drm_displays = detect_wayland_drm_displays()
            if drm_displays:
                return drm_displays
        if displays:
            return displays
    except Exception:
        pass

    # Fallback if neither xrandr nor DRM sysfs returned a name
    return ["eDP-1"]


if __name__ == "__main__":
    print(detect_display_devices())