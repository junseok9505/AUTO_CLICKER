"""현재 OS 에 맞는 어댑터를 골라준다."""

from __future__ import annotations

import platform as _platform

from .base import PlatformAdapter, UnsupportedPlatform

_cached: PlatformAdapter | None = None


def get_adapter() -> PlatformAdapter:
    global _cached
    if _cached is not None:
        return _cached

    system = _platform.system()
    if system == "Windows":
        from .windows import WindowsAdapter

        _cached = WindowsAdapter()
    elif system == "Darwin":
        from .macos import MacAdapter

        _cached = MacAdapter()
    else:
        _cached = UnsupportedPlatform(system or "unknown")
    return _cached


__all__ = ["PlatformAdapter", "get_adapter"]
