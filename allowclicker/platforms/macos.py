"""macOS 구현 (CoreGraphics 를 ctypes 로 직접 호출, pyobjc 불필요).

주의: Windows 에서 먼저 개발했기 때문에 이 파일은 실제 macOS 에서 검증되지 않았다.
맥 작업을 할 때는 이 파일만 확인/보정하면 된다. 필요한 권한은 두 가지다.
  - 화면 기록(Screen Recording): 화면 캡처
  - 손쉬운 사용(Accessibility): 마우스 클릭 주입
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from pathlib import Path

from .base import PlatformAdapter

kCGEventLeftMouseDown = 1
kCGEventLeftMouseUp = 2
kCGEventMouseMoved = 5
kCGMouseButtonLeft = 0
kCGHIDEventTap = 0
kCGEventSourceStateHIDSystemState = 1
kVK_F8 = 100


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def _load_framework(name: str, fallback: str):
    path = ctypes.util.find_library(name) or fallback
    return ctypes.cdll.LoadLibrary(path)


class MacAdapter(PlatformAdapter):
    name = "macos"
    stop_hotkey_label = "F8"
    # macOS 에서 overrideredirect 창은 키 입력/최상위 표시가 불안정하다.
    # Tk 공식 문서에 따르면 -fullscreen 은 Dock 과 메뉴바까지 덮는다.
    overlay_mode = "fullscreen"

    def __init__(self) -> None:
        self._cg = _load_framework(
            "ApplicationServices",
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        )
        self._cf = _load_framework(
            "CoreFoundation",
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
        )
        cg = self._cg
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            CGPoint,
            ctypes.c_uint32,
        ]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        cg.CGDisplayBounds.restype = CGRect
        cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        cg.CGGetActiveDisplayList.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        cg.CGEventSourceKeyState.restype = ctypes.c_bool
        cg.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        self._cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._setup_permission_api()

    def _setup_permission_api(self) -> None:
        """권한 확인 API 준비 (없는 OS 버전에서도 죽지 않게).

        - CGPreflightScreenCaptureAccess: 화면 기록 권한 확인 (10.15+, 프롬프트 없음)
        - CGRequestScreenCaptureAccess: 화면 기록 권한 요청 (시스템 프롬프트 표시)
        - AXIsProcessTrusted: 손쉬운 사용(마우스 제어) 권한 확인 (프롬프트 없음)
        """
        self._preflight_capture = None
        self._request_capture = None
        self._ax_trusted = None
        for lib in (self._cg, None):
            if lib is None:
                break
            for attr, name in (
                ("_preflight_capture", "CGPreflightScreenCaptureAccess"),
                ("_request_capture", "CGRequestScreenCaptureAccess"),
                ("_ax_trusted", "AXIsProcessTrusted"),
            ):
                if getattr(self, attr) is not None:
                    continue
                func = getattr(lib, name, None)
                if func is not None:
                    func.restype = ctypes.c_bool
                    func.argtypes = []
                    setattr(self, attr, func)

    def check_permissions(self) -> list[str]:
        problems: list[str] = []
        if self._preflight_capture is not None:
            try:
                if not self._preflight_capture():
                    problems.append(
                        "화면 기록(Screen Recording) 권한이 없습니다. "
                        "이 상태에서는 오류 없이 배경화면만 캡처되어 버튼을 절대 찾지 못합니다."
                    )
            except Exception:
                pass
        if self._ax_trusted is not None:
            try:
                if not self._ax_trusted():
                    problems.append(
                        "손쉬운 사용(Accessibility) 권한이 없습니다. "
                        "이 상태에서는 클릭이 조용히 무시됩니다(로그에는 클릭으로 찍힘)."
                    )
            except Exception:
                pass
        return problems

    def request_permissions(self) -> bool:
        """화면 기록 권한 요청 프롬프트를 띄운다."""
        if self._request_capture is None:
            return False
        try:
            return bool(self._request_capture())
        except Exception:
            return False

    def virtual_screen_bounds(self, tk_root) -> tuple[int, int, int, int]:
        try:
            count = ctypes.c_uint32(0)
            ids = (ctypes.c_uint32 * 16)()
            if self._cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
                raise OSError("CGGetActiveDisplayList 실패")
            rects = [self._cg.CGDisplayBounds(ids[i]) for i in range(count.value)]
            if not rects:
                raise OSError("활성 디스플레이 없음")
            left = min(r.origin.x for r in rects)
            top = min(r.origin.y for r in rects)
            right = max(r.origin.x + r.size.width for r in rects)
            bottom = max(r.origin.y + r.size.height for r in rects)
            return int(left), int(top), int(right - left), int(bottom - top)
        except Exception:
            return 0, 0, tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()

    def cursor_position(self) -> tuple[int, int]:
        event = self._cg.CGEventCreate(None)
        try:
            point = self._cg.CGEventGetLocation(event)
            return int(point.x), int(point.y)
        finally:
            if event:
                self._cf.CFRelease(event)

    def move_cursor(self, x: int, y: int) -> None:
        fx, fy = float(x), float(y)
        self._cg.CGWarpMouseCursorPosition(CGPoint(fx, fy))
        self._post(kCGEventMouseMoved, fx, fy)

    def press_left(self) -> None:
        # 누름/뗌은 '현재 커서 위치'에서 발생시킨다.
        x, y = self.cursor_position()
        self._post(kCGEventLeftMouseDown, float(x), float(y))
        time.sleep(0.03)
        self._post(kCGEventLeftMouseUp, float(x), float(y))

    def click(self, x: int, y: int) -> None:
        self.move_cursor(x, y)
        self.wait_for_cursor(x, y)
        time.sleep(0.03)
        self.press_left()

    def _post(self, event_type: int, x: float, y: float) -> None:
        event = self._cg.CGEventCreateMouseEvent(
            None, event_type, CGPoint(x, y), kCGMouseButtonLeft
        )
        if not event:
            raise OSError("CGEventCreateMouseEvent 실패 (손쉬운 사용 권한 확인)")
        try:
            self._cg.CGEventPost(kCGHIDEventTap, event)
        finally:
            self._cf.CFRelease(event)

    def stop_hotkey_pressed(self) -> bool:
        try:
            return bool(
                self._cg.CGEventSourceKeyState(
                    kCGEventSourceStateHIDSystemState, kVK_F8
                )
            )
        except Exception:
            return False

    def display_signature(self) -> tuple | None:
        """현재 디스플레이 배치의 지문. 바뀌면 값이 달라진다."""
        try:
            count = ctypes.c_uint32(0)
            ids = (ctypes.c_uint32 * 16)()
            if self._cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0:
                return None
            rects = []
            for i in range(count.value):
                rect = self._cg.CGDisplayBounds(ids[i])
                rects.append(
                    (
                        int(rect.origin.x),
                        int(rect.origin.y),
                        int(rect.size.width),
                        int(rect.size.height),
                    )
                )
            return tuple(sorted(rects))
        except Exception:
            return None

    def config_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "AllowClicker"

    def permission_notes(self) -> list[str]:
        return [
            "시스템 설정 > 개인정보 보호 및 보안 > 화면 기록 에서 이 앱(또는 터미널)을 허용하세요.",
            "시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용 에서도 허용해야 클릭이 동작합니다.",
            "macOS 15 이상에서는 mss 가 쓰는 레거시 화면 캡처 API 때문에 권한 프롬프트가 "
            "반복될 수 있습니다. 그럴 때는 검사 간격을 늘리고, 권한을 터미널이 아니라 "
            "앱 번들에 부여하세요.",
            "이 macOS 구현은 아직 실기 검증 전입니다. 먼저 테스트 모드로 확인하세요.",
        ]
