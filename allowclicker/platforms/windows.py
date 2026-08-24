"""Windows 구현 (ctypes 만 사용, 추가 패키지 없음)."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from pathlib import Path

from .base import PlatformAdapter

user32 = ctypes.WinDLL("user32", use_last_error=True)

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

VK_F8 = 0x77

GA_ROOT = 2
SW_RESTORE = 9

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)

_MONITOR_ENUM_PROC = ctypes.WINFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,  # HMONITOR
    ctypes.c_void_p,  # HDC
    ctypes.POINTER(wintypes.RECT),
    ctypes.c_void_p,  # LPARAM
)
user32.EnumDisplayMonitors.argtypes = (
    ctypes.c_void_p,
    ctypes.c_void_p,
    _MONITOR_ENUM_PROC,
    ctypes.c_void_p,
)
user32.EnumDisplayMonitors.restype = wintypes.BOOL


class WindowsAdapter(PlatformAdapter):
    name = "windows"
    stop_hotkey_label = "F8"

    def prepare(self) -> None:
        """Tk 생성 전에 per-monitor DPI awareness 를 켠다.

        이걸 안 하면 배율 150% 화면에서 Tk 좌표와 실제 픽셀이 어긋나
        엉뚱한 위치를 클릭한다.
        """
        for attempt in (
            lambda: user32.SetProcessDpiAwarenessContext(-4),  # PER_MONITOR_AWARE_V2
            lambda: ctypes.WinDLL("shcore").SetProcessDpiAwareness(2),
            lambda: user32.SetProcessDPIAware(),
        ):
            try:
                if attempt():
                    return
            except Exception:
                continue

    def apply_ui_scaling(self, tk_root) -> float:
        """per-monitor DPI 인식을 켜면 Tk 가 물리 픽셀로 그려서 4K/150% 화면에서
        글자가 작아진다. 창이 놓인 모니터의 DPI 로 Tk scaling 을 보정한다.
        """
        dpi = 96
        try:
            hwnd = int(tk_root.winfo_id())
            for getter in (
                lambda: user32.GetDpiForWindow(hwnd),
                lambda: user32.GetDpiForSystem(),
            ):
                try:
                    value = int(getter())
                except Exception:
                    continue
                if value > 0:
                    dpi = value
                    break
        except Exception:
            return 1.0
        try:
            tk_root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            return 1.0
        return dpi / 96.0

    def virtual_screen_bounds(self, tk_root) -> tuple[int, int, int, int]:
        get = user32.GetSystemMetrics
        width = get(SM_CXVIRTUALSCREEN)
        height = get(SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:  # 아주 예외적인 경우 대비
            return 0, 0, tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()
        return get(SM_XVIRTUALSCREEN), get(SM_YVIRTUALSCREEN), width, height

    def cursor_position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    def _normalized(self, x: int, y: int) -> tuple[int, int]:
        vx, vy, vw, vh = self.virtual_screen_bounds_raw()
        nx = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ny = int(round((y - vy) * 65535 / max(1, vh - 1)))
        return max(0, min(65535, nx)), max(0, min(65535, ny))

    def virtual_screen_bounds_raw(self) -> tuple[int, int, int, int]:
        get = user32.GetSystemMetrics
        return (
            get(SM_XVIRTUALSCREEN),
            get(SM_YVIRTUALSCREEN),
            get(SM_CXVIRTUALSCREEN),
            get(SM_CYVIRTUALSCREEN),
        )

    def _send(self, flags: int, nx: int = 0, ny: int = 0) -> None:
        event = INPUT(type=INPUT_MOUSE)
        event.mi = MOUSEINPUT(nx, ny, 0, flags, 0, 0)
        sent = user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
        if sent != 1:
            raise OSError(ctypes.get_last_error(), "SendInput 실패")

    def move_cursor(self, x: int, y: int) -> None:
        """가상 데스크톱 전체 기준 절대 좌표로 이동.

        SetCursorPos 가 아니라 SendInput 을 쓰는 이유는, 대상 앱이 hover 상태를
        갱신하도록 정상적인 마우스 이동 이벤트를 주기 위해서다.
        """
        nx, ny = self._normalized(int(x), int(y))
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        self._send(flags, nx, ny)

    def press_left(self) -> None:
        self._send(MOUSEEVENTF_LEFTDOWN)
        time.sleep(0.03)
        self._send(MOUSEEVENTF_LEFTUP)

    def click(self, x: int, y: int) -> None:
        self.move_cursor(x, y)
        self.wait_for_cursor(x, y)
        time.sleep(0.03)  # 대상 앱이 hover 상태를 인식할 시간
        self.press_left()

    def stop_hotkey_pressed(self) -> bool:
        return bool(user32.GetAsyncKeyState(VK_F8) & 0x8000)

    def activate_window_at(self, x: int, y: int) -> bool:
        """클릭할 지점의 최상위 창을 전면으로 가져온다.

        Windows 는 다른 프로세스가 임의로 포그라운드를 바꾸는 걸 제한하므로
        (SetForegroundWindow 실패 가능) 성공 여부를 그대로 돌려준다.
        """
        try:
            point = wintypes.POINT(int(x), int(y))
            hwnd = user32.WindowFromPoint(point)
            if not hwnd:
                return False
            root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            if root == user32.GetForegroundWindow():
                return True  # 이미 활성 상태
            if user32.IsIconic(root):
                user32.ShowWindow(root, SW_RESTORE)
            return bool(user32.SetForegroundWindow(root))
        except Exception:
            return False

    def display_signature(self) -> tuple | None:
        """EnumDisplayMonitors 로 현재 모니터 배치를 실시간 조회한다.

        (mss 는 모니터 목록을 캐시하므로 변경 감지에는 쓸 수 없다.)
        """
        rects: list[tuple[int, int, int, int]] = []

        @_MONITOR_ENUM_PROC
        def callback(_hmon, _hdc, rect_ptr, _data):
            rect = rect_ptr.contents
            rects.append((rect.left, rect.top, rect.right, rect.bottom))
            return 1

        try:
            if not user32.EnumDisplayMonitors(None, None, callback, 0):
                return None
        except Exception:
            return None
        return tuple(sorted(rects)) if rects else None

    def config_dir(self) -> Path:
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home()
        return root / "AllowClicker"

    def permission_notes(self) -> list[str]:
        return [
            "관리자 권한으로 실행되는 창(예: 관리자 터미널)을 클릭해야 하면 "
            "이 프로그램도 관리자 권한으로 실행해야 합니다."
        ]
