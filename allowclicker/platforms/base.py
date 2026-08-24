"""OS 별 기능 추상화.

OS 마다 다른 것만 여기에 모았다.
- DPI/좌표 준비 (prepare)
- 전체 가상 화면 범위 (오버레이 창 크기)
- 마우스 이동/클릭/현재 위치
- 전역 중지 핫키 감지
- 설정 파일 위치, 권한 안내 문구

새 OS 지원은 이 클래스를 구현해서 platforms/__init__.py 에 등록하면 끝.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path


class PlatformAdapter(ABC):
    name = "unknown"
    stop_hotkey_label = ""
    # 전체 화면 오버레이 구현 방식
    #   override   : overrideredirect + geometry (Windows/X11)
    #   fullscreen : wm attributes -fullscreen (macOS. Dock/메뉴바까지 덮음)
    overlay_mode = "override"

    def prepare(self) -> None:
        """GUI 생성 전에 호출. DPI 설정 등."""

    def apply_ui_scaling(self, tk_root) -> float:
        """고해상도 화면에서 UI 가 작아지지 않게 Tk 스케일을 맞춘다. 배율 반환."""
        return 1.0

    def check_permissions(self) -> list[str]:
        """부족한 OS 권한 목록. 비어 있으면 정상.

        macOS 처럼 권한이 없어도 오류 없이 조용히 실패하는 OS 를 위해
        실행 전에 미리 확인한다.
        """
        return []

    @abstractmethod
    def virtual_screen_bounds(self, tk_root) -> tuple[int, int, int, int]:
        """오버레이가 덮을 (x, y, width, height). 멀티 모니터 포함."""

    @abstractmethod
    def cursor_position(self) -> tuple[int, int]: ...

    @abstractmethod
    def move_cursor(self, x: int, y: int) -> None: ...

    @abstractmethod
    def press_left(self) -> None:
        """현재 커서 위치에서 좌클릭(누름+뗌)."""

    def click(self, x: int, y: int) -> None:
        """(x, y) 로 이동한 뒤 좌클릭. 커서가 실제로 도착한 것을 확인하고 누른다."""
        self.move_cursor(x, y)
        self.wait_for_cursor(x, y)
        self.press_left()

    def wait_for_cursor(
        self, x: int, y: int, timeout: float = 0.15
    ) -> tuple[int, int]:
        """커서 이동이 실제로 반영될 때까지 기다린 뒤 최종 위치를 돌려준다.

        SendInput/CGEvent 로 보낸 이동은 입력 큐를 거쳐 비동기로 처리된다.
        그래서 이동 직후 커서 좌표를 읽으면 '이동 전 위치'가 나올 수 있다.
        목표 좌표에 도달하거나, 좌표가 두 번 연속 같아질 때까지 폴링한다.
        """
        target = (int(x), int(y))
        deadline = time.monotonic() + max(0.01, timeout)
        previous: tuple[int, int] | None = None
        position = target
        while True:
            try:
                position = self.cursor_position()
            except Exception:
                return target
            if position == target:
                return position
            if previous == position and time.monotonic() >= deadline:
                return position  # 더 안 움직인다 -> 여기가 최종 위치
            previous = position
            if time.monotonic() >= deadline:
                return position
            time.sleep(0.005)

    def stop_hotkey_pressed(self) -> bool:
        """전역 중지 키가 눌렸는지. 지원 안 하면 항상 False."""
        return False

    def activate_window_at(self, x: int, y: int) -> bool:
        """(x, y) 지점의 창을 활성화한다.

        비활성 창의 버튼은 첫 클릭이 창 활성화에만 쓰이고 버려지는 앱이 있다.
        미리 활성화해 두면 한 번의 클릭으로 눌린다. 미지원이면 False.
        """
        return False

    def display_signature(self) -> tuple | None:
        """디스플레이 배치의 지문. 감시 중 해상도/배치 변경 감지에 쓴다.

        None 이면 변경 감지를 건너뛴다.
        """
        return None

    @abstractmethod
    def config_dir(self) -> Path: ...

    def permission_notes(self) -> list[str]:
        """실행 시 필요한 OS 권한 안내."""
        return []


class UnsupportedPlatform(PlatformAdapter):
    """지원하지 않는 OS 에서도 UI 는 뜨도록 하는 안전한 대체 구현."""

    name = "unsupported"

    def __init__(self, system: str) -> None:
        self.system = system

    def virtual_screen_bounds(self, tk_root) -> tuple[int, int, int, int]:
        return 0, 0, tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()

    def cursor_position(self) -> tuple[int, int]:
        return 0, 0

    def move_cursor(self, x: int, y: int) -> None:
        raise NotImplementedError(f"{self.system} 에서는 마우스 제어를 지원하지 않습니다.")

    def press_left(self) -> None:
        raise NotImplementedError(f"{self.system} 에서는 클릭을 지원하지 않습니다.")

    def config_dir(self) -> Path:
        return Path.home() / ".allowclicker"

    def permission_notes(self) -> list[str]:
        return [f"{self.system} 은 아직 지원하지 않습니다. 테스트 모드로만 사용하세요."]
