"""The device catalogue behind the Devices tab.

Each profile is an *emulation* profile: the CSS viewport, device scale factor
(DPR), user agent, and the mobile/touch flags that a device run applies to its
Playwright context. Nothing here touches real hardware; a profile called
"Zebra TC58" drives the bundled Chromium at that handset's viewport with its
UA string, plus the deterministic simulator in app/mobile_runner.py for the
native scan flow. The docs and the Devices tab say exactly that.

Groups: handheld (rugged handsets and legacy phones), android, ios, other
(other phone operating systems), desktop.
"""
from __future__ import annotations

GROUPS = [
    ("handheld", "Handheld & legacy"),
    ("android", "Android"),
    ("ios", "iOS"),
    ("other", "Other OS"),
    ("desktop", "Desktop"),
]

_DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _profile(device_id, name, group, width, height, dpr, user_agent, is_mobile,
             has_touch, blurb):
    return {"id": device_id, "name": name, "group": group,
            "viewport": {"width": width, "height": height},
            "device_scale_factor": dpr, "user_agent": user_agent,
            "is_mobile": is_mobile, "has_touch": has_touch, "blurb": blurb}


DEVICES = [
    # Rugged handsets and legacy phones: the warehouse countertop.
    _profile("zebra-tc21", "Zebra TC21", "handheld", 360, 640, 2,
             "Mozilla/5.0 (Linux; Android 11; TC21) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
             True, True, "5in 720x1280 legacy Zebra touch computer."),
    _profile("zebra-tc58", "Zebra TC58", "handheld", 360, 720, 3,
             "Mozilla/5.0 (Linux; Android 13; TC58) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
             True, True, "6in 1080x2160 Zebra mobile computer with DataWedge scans."),
    _profile("honeywell-ck65", "Honeywell CK65", "handheld", 320, 533, 1.5,
             "Mozilla/5.0 (Linux; Android 10; CK65) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
             True, True, "4in 480x800 legacy Honeywell handheld."),
    # Current Android phones and tablet.
    _profile("pixel-8", "Pixel 8", "android", 360, 800, 3,
             "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
             True, True, "6.2in 1080x2400 Google reference phone."),
    _profile("galaxy-s24", "Galaxy S24", "android", 360, 780, 3,
             "Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
             True, True, "6.2in 1080x2340 Samsung flagship phone."),
    _profile("galaxy-tab-s9", "Galaxy Tab S9", "android", 800, 1280, 2,
             "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
             True, True, "11in 1600x2560 Samsung tablet."),
    # iPhones and iPad.
    _profile("iphone-15", "iPhone 15", "ios", 393, 852, 3,
             "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
             "Mobile/15E148 Safari/604.1",
             True, True, "6.1in 1179x2556 iPhone."),
    _profile("iphone-se", "iPhone SE", "ios", 375, 667, 2,
             "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
             "Mobile/15E148 Safari/604.1",
             True, True, "4.7in 750x1334 small iPhone."),
    _profile("ipad-air", "iPad Air", "ios", 820, 1180, 2,
             "Mozilla/5.0 (iPad; CPU OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
             True, True, "10.9in 1640x2360 iPad."),
    # Other phone operating systems.
    _profile("nokia-8110-kaios", "Nokia 8110 4G (KaiOS)", "other", 240, 320, 1,
             "Mozilla/5.0 (Mobile; Nokia 8110 4G; rv:48.0) Gecko/48.0 "
             "Firefox/48.0 KAIOS/2.5",
             True, False, "2.4in 240x320 KaiOS feature phone with a d-pad, no touch."),
    _profile("ubuntu-touch", "Ubuntu Touch phone", "other", 360, 780, 3,
             "Mozilla/5.0 (Linux; Ubuntu 20.04 like Android 9) AppleWebKit/537.36 "
             "(KHTML, like Gecko) QtWebEngine/5.15.2 Chrome/83.0.4103.122 "
             "Mobile Safari/537.36",
             True, True, "UBports community phone with the Morph browser."),
    # Desktop viewports for the ERP/CRM packs.
    _profile("laptop-1366", "Laptop 1366x768", "desktop", 1366, 768, 1,
             _DESKTOP_UA, False, False, "Commodity Windows laptop viewport."),
    _profile("desktop-1920", "Desktop 1920x1080", "desktop", 1920, 1080, 1,
             _DESKTOP_UA, False, False, "Full-HD office desktop viewport."),
    _profile("desktop-4k", "Desktop 4K", "desktop", 3840, 2160, 2,
             _DESKTOP_UA, False, False, "4K desktop viewport at DPR 2."),
]

_BY_ID = {item["id"]: item for item in DEVICES}


def get(device_id):
    """The catalogue entry for ``device_id``, or None when unknown."""
    return _BY_ID.get(str(device_id or ""))


def context_kwargs(device_id):
    """Playwright ``new_context`` kwargs for ``device_id``.

    Raises KeyError for an unknown id so callers fail closed.
    """
    profile = get(device_id)
    if profile is None:
        raise KeyError(f"Unknown device: {device_id}")
    return {"viewport": dict(profile["viewport"]),
            "device_scale_factor": profile["device_scale_factor"],
            "user_agent": profile["user_agent"],
            "is_mobile": profile["is_mobile"],
            "has_touch": profile["has_touch"]}


def simulator_platform(device_id):
    """Which mobile_runner platform emulates this device's native side."""
    profile = get(device_id) or {}
    group = profile.get("group")
    if group == "handheld":
        return "zebra"
    if group == "ios":
        return "ios"
    return "android"


#: The catalogue as plan-matrix device entries: id -> new_context kwargs.
PLAN_DEVICES = {item["id"]: {"viewport": dict(item["viewport"]),
                             "device_scale_factor": item["device_scale_factor"],
                             "user_agent": item["user_agent"],
                             "is_mobile": item["is_mobile"],
                             "has_touch": item["has_touch"]}
                for item in DEVICES}
