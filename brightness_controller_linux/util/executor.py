#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import glob
import json
import os
import re
import shutil
import subprocess
import threading
from typing import Dict, Optional, Tuple

from brightness_controller_linux.util.check_displays import (
    detect_display_devices,
    is_wayland,
)

_XRANDR_CMD_RE = re.compile(
    r"--output\s+(?P<output>\S+)"
    r"(?:\s+--brightness\s+(?P<brightness>[0-9.]+))?"
    r"(?:\s+--gamma\s+(?P<r>[0-9.]+):(?P<g>[0-9.]+):(?P<b>[0-9.]+))?"
)

_pending_lock = threading.Lock()
_pending_tasks: Dict[str, Tuple[float, float, float, float]] = {}
_worker_running = False
_ddc_map_cache: Optional[Dict[str, int]] = None
_gammastep_proc: Optional[subprocess.Popen] = None
_gnome_nightlight_disabled = False


def _is_internal_panel(output_name: str) -> bool:
    """Return True if the connector name corresponds to an internal laptop panel."""
    upper = output_name.upper()
    return upper.startswith(("EDP", "LVDS", "DSI", "DEFAULT"))


def _apply_mutter_crtc_gamma(
    output_name: str, r: float, g: float, b: float
) -> bool:
    """Set true R:G:B CRTC gamma LUT on GNOME (Mutter) or Cinnamon (Muffin) Wayland."""
    global _gnome_nightlight_disabled
    if not shutil.which("busctl"):
        return False

    # Disable GNOME Night Light once so gsd-color does not overwrite our CRTC LUT
    if not _gnome_nightlight_disabled and shutil.which("gsettings"):
        subprocess.run(
            [
                "gsettings",
                "set",
                "org.gnome.settings-daemon.plugins.color",
                "night-light-enabled",
                "false",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _gnome_nightlight_disabled = True

    for service, obj_path, iface in (
        (
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig",
        ),
        (
            "org.cinnamon.Muffin.DisplayConfig",
            "/org/cinnamon/Muffin/DisplayConfig",
            "org.cinnamon.Muffin.DisplayConfig",
        ),
    ):
        res = subprocess.run(
            [
                "busctl",
                "--user",
                "--json=short",
                "call",
                service,
                obj_path,
                iface,
                "GetResources",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0 or not res.stdout.strip():
            continue

        try:
            payload = json.loads(res.stdout)
            data = payload.get("data", [])
            serial = int(data[0])
            crtcs = data[1]
            outputs = data[2]
        except (ValueError, IndexError, KeyError, TypeError):
            continue

        # Find the CRTC assigned to the target output (or fallback to first active CRTC)
        target_crtc: Optional[int] = None
        for out_entry in outputs:
            # out_entry: [id, winsys_id, current_crtc, possible_crtcs, name, ...]
            if len(out_entry) >= 5 and out_entry[4] == output_name and out_entry[2] >= 0:
                target_crtc = int(out_entry[2])
                break

        if target_crtc is None:
            for crtc_entry in crtcs:
                # crtc_entry: [id, winsys_id, x, y, width, height, current_mode, ...]
                if len(crtc_entry) >= 7 and crtc_entry[6] >= 0:
                    target_crtc = int(crtc_entry[0])
                    break

        if target_crtc is None:
            continue

        # Query hardware gamma LUT size for this CRTC
        gamma_res = subprocess.run(
            [
                "busctl",
                "--user",
                "--json=short",
                "call",
                service,
                obj_path,
                iface,
                "GetCrtcGamma",
                "uu",
                str(serial),
                str(target_crtc),
            ],
            capture_output=True,
            text=True,
        )
        if gamma_res.returncode != 0:
            continue

        try:
            g_data = json.loads(gamma_res.stdout).get("data", [])
            lut_size = len(g_data[0])
        except (ValueError, IndexError, TypeError):
            lut_size = 256

        if lut_size <= 1:
            lut_size = 256

        def _build_ramp(ch: float) -> list:
            ch_clamped = max(0.05, min(1.0, ch))
            ramp = []
            denom = float(lut_size - 1)
            for i in range(lut_size):
                x = i / denom
                # Combine gamma curve with channel gain so sliders visibly dim highlights too
                val = int(round((x ** (1.0 / ch_clamped)) * ch_clamped * 65535))
                ramp.append(str(max(0, min(65535, val))))
            return ramp

        r_ramp = _build_ramp(r)
        g_ramp = _build_ramp(g)
        b_ramp = _build_ramp(b)

        cmd = (
            [
                "busctl",
                "--user",
                "call",
                service,
                obj_path,
                iface,
                "SetCrtcGamma",
                "uuaqaqaq",
                str(serial),
                str(target_crtc),
                str(lut_size),
            ]
            + r_ramp
            + [str(lut_size)]
            + g_ramp
            + [str(lut_size)]
            + b_ramp
        )
        set_res = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        if set_res.returncode == 0:
            return True

    return False


def _apply_gammastep_rgb(
    brightness: float, r: float, g: float, b: float
) -> bool:
    """Apply R:G:B gamma on wlroots compositors (Sway, Hyprland) via gammastep."""
    global _gammastep_proc
    if not shutil.which("gammastep"):
        return False

    # Avoid spawning gammastep on GNOME/KDE where zwlr_gamma_control is unsupported
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if any(de in desktop for de in ("GNOME", "UBUNTU", "KDE", "PLASMA", "CINNAMON")):
        return False

    r_c = max(0.1, min(1.0, r))
    g_c = max(0.1, min(1.0, g))
    b_c = max(0.1, min(1.0, b))

    if _gammastep_proc is not None and _gammastep_proc.poll() is None:
        _gammastep_proc.terminate()
        try:
            _gammastep_proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _gammastep_proc.kill()

    _gammastep_proc = subprocess.Popen(
        [
            "gammastep",
            "-m",
            "wayland",
            "-P",
            "-O",
            "6500",
            "-g",
            f"{r_c:.2f}:{g_c:.2f}:{b_c:.2f}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _has_wl_gammarelay() -> bool:
    """Check if wl-gammarelay-rs is available on the user D-Bus session."""
    if not shutil.which("busctl"):
        return False
    res = subprocess.run(
        ["busctl", "--user", "status", "rs.wl-gammarelay"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return res.returncode == 0


def _apply_wl_gammarelay(
    output: str, brightness: float, r: float, g: float, b: float
) -> bool:
    """Apply software brightness and color temperature via wl-gammarelay-rs."""
    if not _has_wl_gammarelay():
        return False

    obj_name = output.replace("-", "_")
    obj_path = f"/outputs/{obj_name}"

    check = subprocess.run(
        [
            "busctl",
            "--user",
            "get-property",
            "rs.wl-gammarelay",
            obj_path,
            "rs.wl.gammarelay",
            "Brightness",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if check.returncode != 0:
        obj_path = "/"

    clamped_b = max(0.05, min(1.0, brightness))
    subprocess.run(
        [
            "busctl",
            "--user",
            "set-property",
            "rs.wl-gammarelay",
            obj_path,
            "rs.wl.gammarelay",
            "Brightness",
            "d",
            f"{clamped_b:.3f}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    ratio = min(1.0, max(0.1, b / max(r, 0.01)))
    temp = max(1000, min(10000, int(2500 + (ratio * 4000))))
    subprocess.run(
        [
            "busctl",
            "--user",
            "set-property",
            "rs.wl-gammarelay",
            obj_path,
            "rs.wl.gammarelay",
            "Temperature",
            "q",
            str(temp),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _apply_kde_nightcolor(r: float, g: float, b: float) -> bool:
    """Fallback color temperature adjustment for KDE Plasma Wayland (KWin)."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if "KDE" not in desktop and "PLASMA" not in desktop:
        return False

    kwrite = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
    qdbus = (
        shutil.which("qdbus6")
        or shutil.which("qdbus")
        or shutil.which("qdbus-qt5")
    )
    if not kwrite or not qdbus:
        return False

    is_neutral = all(abs(ch - 1.0) <= 0.02 for ch in (r, g, b))
    ratio = min(1.0, max(0.1, (b * g) / max(r, 0.05)))
    temp = max(1500, min(6500, int(1500 + ratio * 5000)))

    for cmd in (
        [kwrite, "--file", "kwinrc", "--group", "NightColor", "--key", "Active", "false" if is_neutral else "true"],
        [kwrite, "--file", "kwinrc", "--group", "NightColor", "--key", "Mode", "Constant"],
        [kwrite, "--file", "kwinrc", "--group", "NightColor", "--key", "NightTemperature", str(temp)],
    ):
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    subprocess.run(
        [qdbus, "org.kde.KWin", "/KWin", "reconfigure"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def _apply_internal_backlight(
    output: str, brightness: float, r: float, g: float, b: float
) -> None:
    """Set hardware backlight brightness and apply Wayland compositor RGB gamma."""
    pct = max(5, min(100, int(round(brightness * 100))))

    if shutil.which("brightnessctl"):
        subprocess.run(
            ["brightnessctl", "--quiet", "set", f"{pct}%"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        bl_dirs = sorted(glob.glob("/sys/class/backlight/*"))
        if bl_dirs and shutil.which("busctl"):
            bl_dev = os.path.basename(bl_dirs[0])
            try:
                with open(
                    os.path.join(bl_dirs[0], "max_brightness"), "r", encoding="utf-8"
                ) as f:
                    max_b = int(f.read().strip())
                raw_val = max(1, int(round((pct / 100.0) * max_b)))
                subprocess.run(
                    [
                        "busctl",
                        "call",
                        "org.freedesktop.login1",
                        "/org/freedesktop/login1/session/auto",
                        "org.freedesktop.login1.Session",
                        "SetBrightness",
                        "ssu",
                        "backlight",
                        bl_dev,
                        str(raw_val),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, ValueError):
                pass

    # Apply RGB channels via compositor LUT (Mutter -> gammastep -> KDE NightColor)
    if _apply_mutter_crtc_gamma(output, r, g, b):
        return
    if _apply_gammastep_rgb(brightness, r, g, b):
        return
    _apply_kde_nightcolor(r, g, b)


def _get_ddc_display_index(output_name: str) -> int:
    """Map an external connector name to a 1-based ddcutil display number."""
    global _ddc_map_cache
    if _ddc_map_cache is None:
        _ddc_map_cache = {}
        all_displays = detect_display_devices()
        ext_displays = [d for d in all_displays if not _is_internal_panel(d)]
        for idx, name in enumerate(ext_displays, start=1):
            _ddc_map_cache[name] = idx
    return _ddc_map_cache.get(output_name, 1)


def _apply_external_ddc(
    output: str, brightness: float, r: float, g: float, b: float
) -> None:
    """Apply brightness (VCP 0x10) and RGB gain (0x16/0x18/0x1A) via ddcutil."""
    if _apply_mutter_crtc_gamma(output, r, g, b) and abs(brightness - 1.0) <= 0.02:
        return

    if not shutil.which("ddcutil"):
        return

    disp_num = str(_get_ddc_display_index(output))
    pct = max(0, min(100, int(round(brightness * 100))))

    subprocess.run(
        [
            "ddcutil",
            "--display",
            disp_num,
            "--noverify",
            "--sleep-multiplier",
            ".1",
            "setvcp",
            "10",
            str(pct),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if any(abs(ch - 1.0) > 0.02 for ch in (r, g, b)):
        for vcp_code, ch_val in (("16", r), ("18", g), ("1A", b)):
            gain = max(10, min(100, int(round(ch_val * 100))))
            subprocess.run(
                [
                    "ddcutil",
                    "--display",
                    disp_num,
                    "--noverify",
                    "--sleep-multiplier",
                    ".1",
                    "setvcp",
                    vcp_code,
                    str(gain),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def _worker_loop() -> None:
    """Drain latest pending display adjustments without blocking the Qt UI thread."""
    global _worker_running
    while True:
        with _pending_lock:
            if not _pending_tasks:
                _worker_running = False
                return
            output, (brightness, r, g, b) = _pending_tasks.popitem()

        if _apply_wl_gammarelay(output, brightness, r, g, b):
            continue

        if _is_internal_panel(output):
            _apply_internal_backlight(output, brightness, r, g, b)
        else:
            _apply_external_ddc(output, brightness, r, g, b)


def _enqueue_wayland_update(
    output: str, brightness: float, r: float, g: float, b: float
) -> None:
    """Queue the latest slider state for a display and start the worker if idle."""
    global _worker_running
    with _pending_lock:
        _pending_tasks[output] = (brightness, r, g, b)
        if not _worker_running:
            _worker_running = True
            t = threading.Thread(target=_worker_loop, daemon=True)
            t.start()


def execute_command(string_cmd: str) -> None:
    """Execute display brightness/gamma command on X11 or translate for Wayland."""
    if not is_wayland():
        try:
            subprocess.check_output(
                string_cmd, shell=True, stderr=subprocess.DEVNULL
            )
            return
        except subprocess.CalledProcessError:
            pass

    match = _XRANDR_CMD_RE.search(string_cmd)
    if not match:
        return

    output = match.group("output")
    brightness = float(match.group("brightness") or 1.0)
    r = float(match.group("r") or 1.0)
    g = float(match.group("g") or 1.0)
    b = float(match.group("b") or 1.0)

    _enqueue_wayland_update(output, brightness, r, g, b)