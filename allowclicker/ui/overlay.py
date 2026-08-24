"""전체 화면 오버레이: 영역 드래그 선택 / 화면 색 추출.

tkinter 만 사용하므로 Windows 와 macOS 에서 같은 코드로 동작한다.
멀티 모니터 범위는 플랫폼 어댑터가 알려준다.
"""

from __future__ import annotations

import tkinter as tk

from ..geometry import Region
from ..platforms.base import PlatformAdapter


class _FullScreenOverlay:
    def __init__(
        self,
        root: tk.Misc,
        adapter: PlatformAdapter,
        alpha: float,
        cursor: str,
        bg: str = "#0b0b12",
        bounds: tuple[int, int, int, int] | None = None,
    ) -> None:
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()

        # 특정 모니터만 덮을 때는 -fullscreen 대신 geometry 로 정확히 맞춘다.
        fullscreen = (
            getattr(adapter, "overlay_mode", "override") == "fullscreen"
            and bounds is None
        )
        if not fullscreen:
            self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        try:
            self.window.attributes("-alpha", alpha)
        except tk.TclError:  # 일부 환경에서 미지원
            pass

        x, y, width, height = bounds or adapter.virtual_screen_bounds(root)
        if fullscreen:
            # macOS: -fullscreen 이 Dock/메뉴바까지 덮어준다. 대신 창이 놓인
            # 디스플레이 한 대만 덮으므로 원점은 창이 뜬 뒤에 다시 읽는다.
            self.window.attributes("-fullscreen", True)
        else:
            self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.origin = (x, y)

        self.canvas = tk.Canvas(
            self.window, bg=bg, highlightthickness=0, cursor=cursor, bd=0
        )
        self.canvas.pack(fill="both", expand=True)
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.window.update_idletasks()
        # 원점은 '요청한 좌표'가 아니라 '실제로 창이 놓인 좌표'를 쓴다.
        # 멀티 모니터에서 음수 좌표 geometry 가 그대로 반영되지 않는 환경이 있어서,
        # 여기서 되읽어야 드래그한 위치와 실제 화면 좌표가 어긋나지 않는다.
        actual = (self.window.winfo_rootx(), self.window.winfo_rooty())
        if actual != self.origin:
            self.origin = actual
        self.width = self.canvas.winfo_width() or width
        self.height = self.canvas.winfo_height() or height
        try:
            self.canvas.grab_set()
        except tk.TclError:
            pass

    def close(self) -> None:
        try:
            self.canvas.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()

    def to_screen(self, event: tk.Event) -> tuple[int, int]:
        return self.origin[0] + int(event.x), self.origin[1] + int(event.y)


def select_region(
    root: tk.Misc,
    adapter: PlatformAdapter,
    bounds: tuple[int, int, int, int] | None = None,
) -> Region | None:
    """드래그로 영역을 선택한다. Esc 또는 우클릭이면 None.

    bounds 를 주면 그 모니터 영역만 덮는다(멀티 모니터에서 안전).
    """
    overlay = _FullScreenOverlay(
        root, adapter, alpha=0.35, cursor="crosshair", bounds=bounds
    )
    canvas = overlay.canvas
    state: dict = {"start": None, "rect": None, "result": None}

    canvas.create_text(
        overlay.width // 2,
        40,
        text="감시할 영역을 마우스로 드래그하세요  (Esc: 취소)",
        fill="#ffffff",
        font=("Segoe UI", 16, "bold"),
    )

    def on_press(event: tk.Event) -> None:
        state["start"] = (event.x, event.y)
        if state["rect"] is not None:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#b388ff", width=2
        )

    def on_drag(event: tk.Event) -> None:
        if state["start"] is None:
            return
        x0, y0 = state["start"]
        canvas.coords(state["rect"], x0, y0, event.x, event.y)

    def on_release(event: tk.Event) -> None:
        if state["start"] is None:
            return
        x0, y0 = state["start"]
        ox, oy = overlay.origin
        region = Region.from_points(ox + x0, oy + y0, *overlay.to_screen(event))
        state["result"] = region if region.is_valid else None
        overlay.close()

    def cancel(_event: tk.Event | None = None) -> None:
        state["result"] = None
        overlay.close()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-2>", cancel)  # macOS 마우스는 우클릭이 2번일 수 있다
    canvas.bind("<Button-3>", cancel)
    overlay.window.bind("<Escape>", cancel)
    canvas.bind("<Escape>", cancel)

    root.wait_window(overlay.window)
    return state["result"]


def pick_screen_point(
    root: tk.Misc,
    adapter: PlatformAdapter,
    bounds: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """화면의 한 점을 클릭해서 좌표를 얻는다 (색 추출/캘리브레이션용).

    오버레이가 화면 색을 바꾸지 않도록 거의 투명하게 띄우고,
    창을 닫은 뒤에 실제 픽셀을 읽는다.
    """
    overlay = _FullScreenOverlay(
        root, adapter, alpha=0.02, cursor="crosshair", bounds=bounds
    )
    state: dict = {"point": None}

    def on_click(event: tk.Event) -> None:
        state["point"] = overlay.to_screen(event)
        overlay.close()

    def cancel(_event: tk.Event | None = None) -> None:
        state["point"] = None
        overlay.close()

    overlay.canvas.bind("<ButtonPress-1>", on_click)
    overlay.canvas.bind("<Button-2>", cancel)
    overlay.canvas.bind("<Button-3>", cancel)
    overlay.window.bind("<Escape>", cancel)
    overlay.canvas.bind("<Escape>", cancel)

    root.wait_window(overlay.window)
    if state["point"] is None:
        return None
    root.update_idletasks()
    root.after(120)  # 오버레이가 완전히 사라진 뒤 색을 읽는다
    return state["point"]
