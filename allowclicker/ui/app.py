"""메인 창.

영역 선택 -> (필요하면) 색 학습 -> 시작.
감시 스레드는 worker.ScanWorker 가 담당하고, UI 는 큐로 결과만 받아 그린다.
"""

from __future__ import annotations

import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import numpy as np

from .. import __version__
from ..capture import ScreenCapture
from ..config import AppConfig, config_path, load_config, save_config
from ..detector import (
    DetectorConfig,
    DetectStats,
    calibrate_at,
    calibrate_from_rect,
    detect,
    detect_auto,
    make_template,
    pick_target,
)
from ..geometry import Region
from ..platforms import get_adapter
from ..worker import ScanWorker, WorkerEvent, downscale
from .overlay import pick_screen_point, select_region

PREVIEW_WIDTH = 460
PREVIEW_HEIGHT = 200
MAX_LOG_LINES = 400


def photo_to_numpy(photo: tk.PhotoImage) -> np.ndarray:
    """tkinter 로 읽은 이미지를 RGB 배열로 (Pillow 없이).

    Tk 8.6 은 PNG/GIF 를 읽을 수 있다. 픽셀은 한 개씩 읽어야 하지만
    버튼 크기(수천 픽셀)라 문제되지 않는다.
    """
    width, height = photo.width(), photo.height()
    if width <= 0 or height <= 0:
        raise ValueError("빈 이미지입니다.")
    if width * height > 400_000:
        raise ValueError("이미지가 너무 큽니다. 버튼만 잘라낸 이미지를 사용하세요.")
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            value = photo.get(x, y)
            if isinstance(value, str):
                value = value.split()
            row.append([int(value[0]), int(value[1]), int(value[2])])
        rows.append(row)
    return np.array(rows, dtype=np.uint8)


def numpy_to_photo(image: np.ndarray) -> tk.PhotoImage:
    """RGB 배열을 PPM(P6) 바이트로 만들어 tkinter 이미지로 변환 (Pillow 불필요).

    Tk 8.6 은 -data 에 들어온 바이너리 PPM 을 그대로 읽는다.
    (base64 는 GIF/PNG 만 지원하므로 여기서는 원본 바이트를 넘긴다.)
    """
    height, width = image.shape[:2]
    header = f"P6 {width} {height} 255 ".encode("ascii")
    body = np.ascontiguousarray(image, dtype=np.uint8).tobytes()
    return tk.PhotoImage(data=header + body)


class AllowClickerApp:
    def __init__(self) -> None:
        self.adapter = get_adapter()
        self.adapter.prepare()  # Tk 생성 전에 DPI 설정
        self.config_dir = self.adapter.config_dir()
        self.cfg: AppConfig = load_config(self.config_dir)
        self.capture = ScreenCapture()
        self.events: "queue.Queue[WorkerEvent]" = queue.Queue()
        self.worker: ScanWorker | None = None
        self._photo: tk.PhotoImage | None = None
        # (위젯, 활성 상태) - 감시 중에는 설정 위젯을 잠근다
        self._settings_widgets: list[tuple[tk.Widget, str]] = []

        self.root = tk.Tk()
        self.root.title(f"Allow 자동 클릭기 v{__version__}")
        self.root.resizable(False, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.ui_scale = self.adapter.apply_ui_scaling(self.root)
        # 고해상도 화면에서는 미리보기도 같이 키운다
        self.preview_w = int(PREVIEW_WIDTH * self.ui_scale)
        self.preview_h = int(PREVIEW_HEIGHT * self.ui_scale)

        self.region: Region | None = self.cfg.region
        self._build_vars()
        self._build_ui()
        self._apply_config_to_ui()
        self._pump()

        self._log(
            f"플랫폼: {self.adapter.name}  UI 배율: {self.ui_scale:.2f}  "
            f"설정파일: {config_path(self.config_dir)}"
        )
        for note in self.adapter.permission_notes():
            self._log(f"안내: {note}")
        for problem in self.adapter.check_permissions():
            self._log(f"권한 경고: {problem}")
        self._log(
            "주의: 승인 버튼을 자동으로 누르므로, 확인 없이 실행되면 곤란한 작업이 "
            "있을 때는 테스트 모드로 먼저 확인하세요."
        )

    # ---------------------------------------------------------------- UI 구성
    def _build_vars(self) -> None:
        cfg, det = self.cfg, self.cfg.detector
        self.region_var = tk.StringVar()
        self.status_var = tk.StringVar(value="정지")
        self.clicks_var = tk.StringVar(value="클릭 0회")
        self.color_var = tk.StringVar(value="-")

        self.interval_var = tk.StringVar(value=f"{cfg.interval:g}")
        self.cooldown_var = tk.StringVar(value=f"{cfg.cooldown:g}")
        self.confirm_var = tk.StringVar(value=str(cfg.confirm_frames))
        self.max_clicks_var = tk.StringVar(value=str(cfg.max_clicks))
        self.dry_var = tk.BooleanVar(value=cfg.dry_run)
        self.restore_var = tk.BooleanVar(value=cfg.restore_cursor)
        self.policy_var = tk.StringVar(value=cfg.click_policy)
        self.topmost_var = tk.BooleanVar(value=False)
        self.retries_var = tk.StringVar(value=str(cfg.max_retries))
        self.timeout_var = tk.StringVar(value=f"{cfg.retry_timeout:g}")
        self.activate_var = tk.BooleanVar(value=cfg.activate_before_click)
        self.auto_calib_var = tk.BooleanVar(value=cfg.auto_calibrate)
        self.auto_offset_var = tk.BooleanVar(value=cfg.auto_offset)
        self.offset_var = tk.StringVar()
        self.monitor_var = tk.StringVar()
        self.monitors: list[dict] = []  # mss 좌표계 기준 모니터 목록
        self.template_var = tk.StringVar()
        self.match_var = tk.StringVar(value=f"{det.min_shape_match:g}")

        self.hue_var = tk.StringVar(value=f"{det.hue_center:g}")
        self.tol_var = tk.StringVar(value=f"{det.hue_tolerance:g}")
        self.sat_var = tk.StringVar(value=f"{det.sat_min:g}")
        self.val_var = tk.StringVar(value=f"{det.val_min:g}")
        self.min_w_var = tk.StringVar(value=str(det.min_width))
        self.max_w_var = tk.StringVar(value=str(det.max_width))
        self.min_h_var = tk.StringVar(value=str(det.min_height))
        self.max_h_var = tk.StringVar(value=str(det.max_height))
        self.fill_var = tk.StringVar(value=f"{det.min_fill:g}")
        self.require_text_var = tk.BooleanVar(value=det.require_text)

    def _entry(
        self, parent: tk.Widget, label: str, var: tk.StringVar, row: int, col: int
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=col * 2, sticky="e", padx=(8, 4), pady=3
        )
        entry = ttk.Entry(parent, textvariable=var, width=7)
        entry.grid(row=row, column=col * 2 + 1, sticky="w", pady=3)
        self._settings_widgets.append((entry, "normal"))

    def _build_ui(self) -> None:
        root = self.root
        pad = {"padx": 10, "pady": 6}

        # --- 0. 모니터
        screen = ttk.LabelFrame(root, text="0. 모니터 (구성이 바뀌면 새로고침)")
        screen.grid(row=0, column=0, sticky="ew", **pad)
        self.monitor_combo = ttk.Combobox(
            screen, textvariable=self.monitor_var, state="readonly", width=34
        )
        self.monitor_combo.grid(row=0, column=0, padx=(8, 4), pady=6, sticky="w")
        refresh = ttk.Button(screen, text="새로고침", command=self._refresh_monitors)
        refresh.grid(row=0, column=1, padx=4)
        whole = ttk.Button(
            screen, text="이 모니터 전체 감시", command=self._use_monitor_region
        )
        whole.grid(row=0, column=2, padx=(4, 8))
        self._settings_widgets += [
            (self.monitor_combo, "readonly"),
            (refresh, "normal"),
            (whole, "normal"),
        ]

        # --- 1. 영역
        area = ttk.LabelFrame(root, text="1. 감시 영역")
        area.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(area, textvariable=self.region_var, width=40).grid(
            row=0, column=0, sticky="w", padx=8, pady=6
        )
        self.select_btn = ttk.Button(area, text="영역 선택", command=self._select_region)
        self.select_btn.grid(row=0, column=1, padx=4)
        self.test_btn = ttk.Button(area, text="한 번 검사", command=self._test_once)
        self.test_btn.grid(row=0, column=2, padx=(4, 8))
        self._settings_widgets.append((self.select_btn, "normal"))

        # --- 2. 색상/캘리브레이션
        color = ttk.LabelFrame(root, text="2. 버튼 인식 기준")
        color.grid(row=2, column=0, sticky="ew", **pad)
        self.swatch = tk.Canvas(
            color, width=44, height=24, highlightthickness=1, highlightbackground="#888"
        )
        self.swatch.grid(row=0, column=0, padx=(8, 6), pady=6)
        ttk.Label(color, textvariable=self.color_var, width=16).grid(row=0, column=1)
        calib = ttk.Button(
            color, text="캘리브레이션 (버튼 직접 클릭)", command=self._calibrate
        )
        calib.grid(row=0, column=2, padx=4)
        reset = ttk.Button(color, text="기본값", command=self._reset_detector)
        reset.grid(row=0, column=3, padx=(4, 8))
        self._settings_widgets += [(calib, "normal"), (reset, "normal")]
        self._entry(color, "색상(°)", self.hue_var, 0, 3)
        self._entry(color, "허용(±°)", self.tol_var, 0, 4)

        # 눌러야 하는 버튼을 직접 지정 (견본 등록)
        ttk.Label(color, textvariable=self.template_var, width=30).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4)
        )
        pick_btn = ttk.Button(
            color, text="버튼 영역 지정", command=self._select_button_rect
        )
        pick_btn.grid(row=1, column=2, padx=4, pady=(0, 4))
        from_file = ttk.Button(
            color, text="이미지로 지정", command=self._template_from_file
        )
        from_file.grid(row=1, column=3, padx=4, pady=(0, 4))
        clear_btn = ttk.Button(color, text="견본 지우기", command=self._clear_template)
        clear_btn.grid(row=1, column=4, padx=(4, 8), pady=(0, 4))
        self._entry(color, "일치도 최소", self.match_var, 1, 3)
        self._settings_widgets += [
            (pick_btn, "normal"),
            (from_file, "normal"),
            (clear_btn, "normal"),
        ]
        ttk.Label(
            color,
            text="'버튼 영역 지정'으로 눌러야 하는 버튼을 감싸면 그 모양까지 비교해서 "
            "비슷한 다른 버튼을 걸러냅니다.",
        ).grid(row=2, column=0, columnspan=10, sticky="w", padx=8, pady=(0, 6))

        # --- 3. 동작
        run = ttk.LabelFrame(root, text="3. 동작 설정")
        run.grid(row=3, column=0, sticky="ew", **pad)
        self._entry(run, "검사 간격(초)", self.interval_var, 0, 0)
        self._entry(run, "클릭 후 대기(초)", self.cooldown_var, 0, 1)
        self._entry(run, "연속 감지 횟수", self.confirm_var, 1, 0)
        self._entry(run, "최대 클릭(0=무제한)", self.max_clicks_var, 1, 1)
        self._entry(run, "재시도(0=사라질 때까지)", self.retries_var, 2, 0)
        self._entry(run, "한 버튼 최대(초)", self.timeout_var, 2, 1)

        auto_calib = ttk.Checkbutton(
            run,
            text="시작할 때 자동 캘리브레이션 (버튼을 스스로 찾아 기준 학습)",
            variable=self.auto_calib_var,
        )
        auto_calib.grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        auto_offset = ttk.Checkbutton(
            run,
            text="클릭 좌표 자동 보정 (버튼이 안 사라지면 커서 위치로 학습)",
            variable=self.auto_offset_var,
        )
        auto_offset.grid(row=4, column=0, columnspan=3, sticky="w", padx=8, pady=2)
        ttk.Label(run, textvariable=self.offset_var).grid(
            row=4, column=3, sticky="w", padx=(4, 8)
        )
        self._settings_widgets += [(auto_calib, "normal"), (auto_offset, "normal")]

        dry = ttk.Checkbutton(
            run, text="테스트 모드 (클릭하지 않고 감지만)", variable=self.dry_var
        )
        dry.grid(row=5, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        activate = ttk.Checkbutton(
            run,
            text="클릭 전에 대상 창 활성화 (비활성 창은 첫 클릭이 무시될 수 있음)",
            variable=self.activate_var,
        )
        activate.grid(row=6, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        restore = ttk.Checkbutton(
            run, text="클릭 후 마우스 원래 위치로 복귀", variable=self.restore_var
        )
        restore.grid(row=7, column=0, columnspan=4, sticky="w", padx=8, pady=2)
        top = ttk.Checkbutton(
            run, text="이 창을 항상 위에 표시", variable=self.topmost_var,
            command=self._apply_topmost,
        )
        top.grid(row=8, column=0, columnspan=4, sticky="w", padx=8, pady=(2, 6))
        self._settings_widgets += [
            (dry, "normal"),
            (restore, "normal"),
            (activate, "normal"),
        ]

        # --- 4. 고급
        adv = ttk.LabelFrame(root, text="4. 고급 (캘리브레이션이 자동으로 채웁니다)")
        adv.grid(row=4, column=0, sticky="ew", **pad)
        self._entry(adv, "최소 채도", self.sat_var, 0, 0)
        self._entry(adv, "최소 명도", self.val_var, 0, 1)
        self._entry(adv, "최소 채움율", self.fill_var, 0, 2)
        self._entry(adv, "가로 최소", self.min_w_var, 1, 0)
        self._entry(adv, "가로 최대", self.max_w_var, 1, 1)
        self._entry(adv, "세로 최소", self.min_h_var, 1, 2)
        self._entry(adv, "세로 최대", self.max_h_var, 2, 0)
        text_chk = ttk.Checkbutton(
            adv, text="버튼 안에 밝은 글자가 있어야 인식", variable=self.require_text_var
        )
        text_chk.grid(row=3, column=0, columnspan=6, sticky="w", padx=8, pady=(2, 6))
        policy_label = ttk.Label(adv, text="여러 개 감지 시")
        policy_label.grid(row=2, column=2, sticky="e", padx=(8, 4))
        policy = ttk.Combobox(
            adv,
            textvariable=self.policy_var,
            values=("match", "leftmost", "score"),
            state="readonly",
            width=9,
        )
        policy.grid(row=2, column=3, sticky="w")
        self._settings_widgets += [(text_chk, "normal"), (policy, "readonly")]

        # --- 5. 제어
        control = ttk.Frame(root)
        control.grid(row=5, column=0, sticky="ew", **pad)
        self.start_btn = ttk.Button(control, text="시작", command=self._start)
        self.start_btn.grid(row=0, column=0, padx=(8, 4))
        self.stop_btn = ttk.Button(
            control, text="정지", command=self._stop, state="disabled"
        )
        self.stop_btn.grid(row=0, column=1, padx=4)
        ttk.Button(control, text="설정 저장", command=self._save).grid(
            row=0, column=2, padx=4
        )
        ttk.Label(control, textvariable=self.status_var, width=34).grid(
            row=0, column=3, padx=8, sticky="w"
        )
        ttk.Label(control, textvariable=self.clicks_var).grid(row=0, column=4)

        hotkey = self.adapter.stop_hotkey_label
        if hotkey:
            ttk.Label(root, text=f"실행 중 {hotkey} 키를 누르면 즉시 정지합니다.").grid(
                row=6, column=0, sticky="w", padx=18
            )

        # --- 6. 미리보기 / 로그
        preview = ttk.LabelFrame(root, text="미리보기 (초록 사각형 = 클릭 대상)")
        preview.grid(row=7, column=0, sticky="ew", **pad)
        self.preview = tk.Canvas(
            preview, width=self.preview_w, height=self.preview_h, bg="#1b1b23",
            highlightthickness=0,
        )
        self.preview.pack(padx=8, pady=8)

        log_frame = ttk.LabelFrame(root, text="로그")
        log_frame.grid(row=8, column=0, sticky="nsew", **pad)
        self.log_text = ScrolledText(log_frame, width=64, height=7, state="disabled")
        self.log_text.pack(padx=8, pady=8, fill="both", expand=True)
        root.rowconfigure(8, weight=1)
        root.columnconfigure(0, weight=1)

    def _apply_config_to_ui(self) -> None:
        self._update_region_label()
        self._update_swatch()
        self._update_offset_label()
        self._update_template_label()
        self._refresh_monitors(quiet=True)

    # -------------------------------------------------------------- 헬퍼/상태
    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _update_region_label(self) -> None:
        self.region_var.set(
            f"선택됨: {self.region}" if self.region else "아직 선택되지 않았습니다"
        )

    def _update_offset_label(self) -> None:
        dx, dy = self.cfg.click_offset_x, self.cfg.click_offset_y
        self.offset_var.set("보정 없음" if (dx, dy) == (0, 0) else f"보정 {dx:+d}, {dy:+d}")

    def _update_swatch(self) -> None:
        det = self.cfg.detector
        rgb = _hsv_to_rgb(det.hue_center, 0.62, 0.95)
        hex_color = "#%02x%02x%02x" % rgb
        self.swatch.configure(bg=hex_color)
        self.color_var.set(f"{hex_color}  ({det.hue_center:g}°)")

    def _apply_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def _set_settings_enabled(self, enabled: bool) -> None:
        for widget, active_state in self._settings_widgets:
            try:
                widget.configure(state=active_state if enabled else "disabled")
            except tk.TclError:
                pass

    # ------------------------------------------------------------ 설정 수집
    def _collect(self, silent: bool = False) -> bool:
        """UI 값을 self.cfg 로 옮긴다. 형식이 틀리면 False."""
        try:
            cfg, det = self.cfg, self.cfg.detector
            cfg.interval = _clamp(float(self.interval_var.get()), 0.05, 10.0)
            cfg.cooldown = _clamp(float(self.cooldown_var.get()), 0.1, 60.0)
            cfg.confirm_frames = int(_clamp(int(self.confirm_var.get()), 1, 10))
            cfg.max_clicks = max(0, int(self.max_clicks_var.get()))
            cfg.max_retries = int(_clamp(int(self.retries_var.get()), 0, 50))
            cfg.retry_timeout = _clamp(float(self.timeout_var.get()), 2.0, 300.0)
            cfg.auto_calibrate = bool(self.auto_calib_var.get())
            cfg.auto_offset = bool(self.auto_offset_var.get())
            cfg.dry_run = bool(self.dry_var.get())
            cfg.restore_cursor = bool(self.restore_var.get())
            cfg.activate_before_click = bool(self.activate_var.get())
            cfg.click_policy = self.policy_var.get() or "leftmost"
            cfg.monitor_index = self._monitor_index()
            cfg.region = self.region

            det.hue_center = float(self.hue_var.get()) % 360.0
            det.hue_tolerance = _clamp(float(self.tol_var.get()), 1.0, 90.0)
            det.sat_min = _clamp(float(self.sat_var.get()), 0.0, 1.0)
            det.val_min = _clamp(float(self.val_var.get()), 0.0, 1.0)
            det.min_fill = _clamp(float(self.fill_var.get()), 0.0, 1.0)
            det.min_width = int(self.min_w_var.get())
            det.max_width = int(self.max_w_var.get())
            det.min_height = int(self.min_h_var.get())
            det.max_height = int(self.max_h_var.get())
            det.require_text = bool(self.require_text_var.get())
            det.min_shape_match = _clamp(float(self.match_var.get()), -1.0, 1.0)
        except (TypeError, ValueError) as exc:
            if not silent:
                messagebox.showerror("설정 오류", f"숫자 입력을 확인하세요.\n{exc}")
            return False

        if det.min_width > det.max_width or det.min_height > det.max_height:
            if not silent:
                messagebox.showerror("설정 오류", "크기 최소값이 최대값보다 큽니다.")
            return False
        self._update_swatch()
        return True

    def _save(self, quiet: bool = False) -> None:
        """설정을 즉시 파일에 저장한다. 다음 실행 때 그대로 복원된다."""
        if not self._collect(silent=quiet):
            return
        try:
            path = save_config(self.config_dir, self.cfg)
        except OSError as exc:
            self._log(f"설정 저장 실패: {exc}")
            return
        if not quiet:
            self._log(f"설정을 저장했습니다: {path}")

    # -------------------------------------------------------------- 모니터
    def _refresh_monitors(self, quiet: bool = False) -> None:
        """모니터 목록을 다시 읽는다. 해상도/배치가 바뀌면 눌러야 한다."""
        try:
            self.monitors = self.capture.monitors(refresh=True)
        except Exception as exc:
            self._log(f"모니터 목록을 읽지 못했습니다: {exc}")
            return

        labels = []
        for index, mon in enumerate(self.monitors):
            if index == 0:
                labels.append(
                    f"전체 화면 {mon['width']}x{mon['height']} "
                    f"({mon['left']}, {mon['top']})"
                )
            else:
                # Windows/macOS 모두 주 모니터의 원점이 (0, 0) 이다.
                primary = " (주)" if mon["left"] == 0 and mon["top"] == 0 else ""
                labels.append(
                    f"모니터 {index}{primary}: {mon['width']}x{mon['height']} "
                    f"({mon['left']}, {mon['top']})"
                )
        self.monitor_combo.configure(values=labels)
        index = self.cfg.monitor_index if self.cfg.monitor_index < len(labels) else 0
        self.monitor_var.set(labels[index] if labels else "")
        if not quiet:
            self._log(f"모니터 {len(self.monitors) - 1}대 감지: " + " | ".join(labels[1:]))
        self._validate_region()

    def _monitor_index(self) -> int:
        try:
            return max(0, self.monitor_combo.current())
        except Exception:
            return 0

    def _selected_monitor(self) -> dict | None:
        if not self.monitors:
            return None
        index = self._monitor_index()
        return self.monitors[index if index < len(self.monitors) else 0]

    def _monitor_at(self, x: int, y: int) -> dict | None:
        """(x, y) 를 포함하는 개별 모니터."""
        for mon in self.monitors[1:]:
            if (
                mon["left"] <= x < mon["left"] + mon["width"]
                and mon["top"] <= y < mon["top"] + mon["height"]
            ):
                return mon
        return self.monitors[0] if self.monitors else None

    def _use_monitor_region(self) -> None:
        mon = self._selected_monitor()
        if mon is None:
            messagebox.showwarning("모니터 없음", "먼저 새로고침을 눌러 주세요.")
            return
        self.region = Region(mon["left"], mon["top"], mon["width"], mon["height"])
        self.cfg.region = self.region
        self._update_region_label()
        self._save(quiet=True)
        self._log(f"모니터 전체를 감시 영역으로 설정: {self.region}")
        if self._window_overlaps_region():
            self._log(
                "참고: 이 창이 감시 영역 안에 있습니다. 다른 모니터로 옮기거나 "
                "영역을 좁히는 편이 안전합니다."
            )
        self._test_once(quiet=True)

    def _validate_region(self) -> None:
        """저장된 영역이 지금 모니터 배치 안에 있는지 확인한다."""
        if self.region is None or not self.monitors:
            return
        whole = self.monitors[0]
        inside = (
            self.region.x >= whole["left"]
            and self.region.y >= whole["top"]
            and self.region.right <= whole["left"] + whole["width"]
            and self.region.bottom <= whole["top"] + whole["height"]
        )
        if not inside:
            self._log(
                f"경고: 저장된 영역 {self.region} 이 현재 화면 범위를 벗어났습니다. "
                "영역을 다시 선택하세요."
            )
            self.region_var.set(f"범위 벗어남: {self.region} (재선택 필요)")

    # ------------------------------------------------------------ 동작 핸들러
    def _select_region(self) -> None:
        monitor = self._selected_monitor()
        bounds = None
        if monitor is not None and self._monitor_index() > 0:
            bounds = (
                monitor["left"],
                monitor["top"],
                monitor["width"],
                monitor["height"],
            )
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)  # 자기 창이 캡처/선택을 방해하지 않도록
        try:
            region = select_region(self.root, self.adapter, bounds)
        finally:
            self.root.deiconify()
            self.root.lift()
        if region is None:
            self._log("영역 선택을 취소했습니다.")
            return
        self.region = region
        self.cfg.region = region
        self._update_region_label()
        self._log(f"영역 선택: {region}")
        self._save(quiet=True)  # 바로 저장해서 다음 실행에도 유지
        self._test_once(quiet=True)

    # ------------------------------------------------- 버튼 견본(클릭 대상) 지정
    def _update_template_label(self) -> None:
        template = self.cfg.template
        if template is None:
            self.template_var.set("버튼 견본: 없음 (색/모양 자동 판단)")
        else:
            where = f" @ {self.cfg.button_rect}" if self.cfg.button_rect else ""
            self.template_var.set(f"버튼 견본: {template.width}x{template.height}{where}")

    def _select_button_rect(self) -> None:
        """눌러야 하는 버튼을 드래그로 감싸서 견본으로 등록한다."""
        messagebox.showinfo(
            "버튼 영역 지정",
            "확인을 누른 뒤, 눌러야 하는 버튼(보라색 Allow)을 드래그로 감싸세요.\n"
            "버튼 테두리에 최대한 딱 맞게 잡으면 정확도가 올라갑니다.\n"
            "Esc 를 누르면 취소됩니다.",
        )
        monitor = self._selected_monitor()
        bounds = None
        if monitor is not None and self._monitor_index() > 0:
            bounds = (
                monitor["left"], monitor["top"], monitor["width"], monitor["height"],
            )
        self.root.withdraw()
        self.root.update()
        time.sleep(0.15)
        try:
            rect = select_region(self.root, self.adapter, bounds)
        finally:
            self.root.deiconify()
            self.root.lift()
        if rect is None:
            self._log("버튼 영역 지정을 취소했습니다.")
            return

        try:
            image, scale = self.capture.grab(rect)
        except Exception as exc:
            messagebox.showerror("버튼 영역 지정 실패", f"화면 캡처 실패: {exc}")
            return

        template = make_template(image)
        if template is None:
            messagebox.showerror("버튼 영역 지정 실패", "영역이 너무 작습니다.")
            return

        # 사용자가 경계를 직접 알려줬으므로 그 영역 자체를 측정한다.
        result = calibrate_from_rect(image, self.cfg.detector)
        self.cfg.button_rect = rect
        self.cfg.template = template
        if result is not None:
            self.cfg.detector = result.config
            self._log(f"버튼 영역 지정: {rect} / {result.describe()}")
            for warning in result.warnings:
                self._log(f"  주의: {warning}")
        else:
            self._log(
                f"버튼 영역 지정: {rect} (색 측정은 실패했지만 견본 모양은 등록했습니다)"
            )
        self.cfg.click_policy = "match"
        self.policy_var.set("match")
        self._detector_to_ui()
        self._update_template_label()

        # 감시 영역이 없거나 버튼을 포함하지 않으면 버튼 주변으로 잡아준다.
        needs_region = self.region is None or not self._region_contains(rect)
        if needs_region or messagebox.askyesno(
            "감시 영역",
            "감시 영역도 이 버튼 주변으로 다시 잡을까요?\n"
            "아니오를 누르면 현재 영역을 유지합니다.",
        ):
            margin_x = max(40, rect.width)
            margin_y = max(30, rect.height * 2)
            self.region = Region(
                rect.x - margin_x,
                rect.y - margin_y,
                rect.width + margin_x * 2,
                rect.height + margin_y * 2,
            )
            self.cfg.region = self.region
            self._update_region_label()
            self._log(f"감시 영역: {self.region}")

        self._save(quiet=True)
        self._test_once(quiet=True)

    def _region_contains(self, rect: Region) -> bool:
        if self.region is None:
            return False
        return (
            rect.x >= self.region.x
            and rect.y >= self.region.y
            and rect.right <= self.region.right
            and rect.bottom <= self.region.bottom
        )

    def _template_from_file(self) -> None:
        """PNG/GIF 이미지 파일을 버튼 견본으로 등록한다 (스크린샷 잘라낸 것 등)."""
        path = filedialog.askopenfilename(
            title="버튼 견본 이미지 선택",
            filetypes=[("PNG/GIF 이미지", "*.png *.gif"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            photo = tk.PhotoImage(file=path)
            image = photo_to_numpy(photo)
        except Exception as exc:
            messagebox.showerror(
                "이미지 읽기 실패",
                f"{exc}\n\nPNG 또는 GIF 만 지원합니다 (JPEG 는 지원하지 않습니다).",
            )
            return

        template = make_template(image)
        if template is None:
            messagebox.showerror("견본 등록 실패", "이미지가 너무 작습니다.")
            return
        result = calibrate_from_rect(image, self.cfg.detector)
        self.cfg.template = template
        self.cfg.button_rect = None  # 파일로 등록한 견본은 위치 정보가 없다
        if result is not None:
            self.cfg.detector = result.config
            self._log(f"이미지로 견본 등록: {Path(path).name} / {result.describe()}")
        else:
            self._log(f"이미지로 견본 등록: {Path(path).name} (모양만 등록)")
        self.cfg.click_policy = "match"
        self.policy_var.set("match")
        self._detector_to_ui()
        self._update_template_label()
        self._save(quiet=True)
        if self.region is not None:
            self._test_once(quiet=True)

    def _clear_template(self) -> None:
        self.cfg.template = None
        self.cfg.button_rect = None
        self.cfg.click_policy = "leftmost"
        self.policy_var.set("leftmost")
        self._update_template_label()
        self._log("버튼 견본을 지웠습니다. 색/모양 자동 판단으로 돌아갑니다.")
        self._save(quiet=True)

    def _calibrate(self) -> None:
        """실제 버튼을 클릭해서 색/크기/채움율/글자비율을 직접 측정한다."""
        messagebox.showinfo(
            "캘리브레이션",
            "확인을 누른 뒤, 인식하려는 버튼(보라색 Allow) 중앙을 클릭하세요.\n"
            "그 버튼을 실제로 측정해서 인식 기준을 자동으로 맞춥니다.\n"
            "Esc 를 누르면 취소됩니다.",
        )
        self.root.withdraw()
        self.root.update()
        try:
            point = pick_screen_point(self.root, self.adapter)
        finally:
            self.root.deiconify()
            self.root.lift()
        if point is None:
            self._log("캘리브레이션을 취소했습니다.")
            return

        # 클릭 지점 주변만 잘라서 측정한다 (모니터 경계를 넘지 않게 자름).
        crop_w, crop_h = 480, 320
        mon = self._monitor_at(*point)
        left, top = point[0] - crop_w // 2, point[1] - crop_h // 2
        if mon:
            left = max(mon["left"], min(left, mon["left"] + mon["width"] - crop_w))
            top = max(mon["top"], min(top, mon["top"] + mon["height"] - crop_h))
        crop = Region(left, top, crop_w, crop_h)
        try:
            image, scale = self.capture.grab(crop)
        except Exception as exc:
            messagebox.showerror("캘리브레이션 실패", f"화면 캡처 실패: {exc}")
            return

        local = (
            int((point[0] - crop.x) * scale),
            int((point[1] - crop.y) * scale),
        )
        result = calibrate_at(image, local, self.cfg.detector)
        if result is None:
            messagebox.showerror(
                "캘리브레이션 실패",
                "클릭한 지점에서 버튼을 찾지 못했습니다.\n"
                "버튼 중앙(글자가 아닌 배경색 부분)을 다시 클릭해 보세요.",
            )
            return

        self.cfg.detector = result.config
        self._detector_to_ui()
        self._log(f"캘리브레이션 완료: {result.describe()}")
        for warning in result.warnings:
            self._log(f"  주의: {warning}")

        # 측정한 버튼 주변을 감시 영역으로 쓸지 물어본다 (좁을수록 빠르고 안전).
        margin_x = max(40, result.width)
        margin_y = max(30, result.height * 2)
        suggested = Region(
            int(crop.x + (result.x - margin_x) / scale),
            int(crop.y + (result.y - margin_y) / scale),
            int((result.width + margin_x * 2) / scale),
            int((result.height + margin_y * 2) / scale),
        )
        if messagebox.askyesno(
            "감시 영역",
            f"이 버튼 주변을 감시 영역으로 설정할까요?\n{suggested}\n\n"
            "아니오를 누르면 기존 영역을 유지합니다.",
        ):
            self.region = suggested
            self.cfg.region = suggested
            self._update_region_label()
            self._log(f"감시 영역을 버튼 주변으로 설정: {suggested}")

        self._save(quiet=True)
        self._test_once(quiet=True)

    def _reset_detector(self) -> None:
        """인식 기준과 좌표 보정을 기본값으로 되돌린다 (튜닝이 꼬였을 때)."""
        self.cfg.detector = DetectorConfig()
        self.cfg.click_offset_x = 0
        self.cfg.click_offset_y = 0
        self._detector_to_ui()
        self._update_offset_label()
        self._log("인식 기준과 클릭 보정을 기본값으로 되돌렸습니다.")
        if self.region is not None:
            self._test_once(quiet=True)

    def _detector_to_ui(self) -> None:
        det = self.cfg.detector
        self.hue_var.set(f"{det.hue_center:g}")
        self.tol_var.set(f"{det.hue_tolerance:g}")
        self.sat_var.set(f"{det.sat_min:g}")
        self.val_var.set(f"{det.val_min:g}")
        self.min_w_var.set(str(det.min_width))
        self.max_w_var.set(str(det.max_width))
        self.min_h_var.set(str(det.min_height))
        self.max_h_var.set(str(det.max_height))
        self.fill_var.set(f"{det.min_fill:g}")
        self.require_text_var.set(det.require_text)
        self.match_var.set(f"{det.min_shape_match:g}")
        self._update_swatch()

    def _test_once(self, quiet: bool = False) -> None:
        if self.region is None:
            if not quiet:
                messagebox.showwarning("영역 필요", "먼저 감시 영역을 선택하세요.")
            return
        if not self._collect():
            return
        try:
            image, scale = self.capture.grab(self.region)
        except Exception as exc:
            messagebox.showerror("캡처 실패", str(exc))
            return
        stats = DetectStats()
        template = self.cfg.template
        detections = detect(image, self.cfg.detector, stats, template=template)
        policy = "match" if template is not None else self.cfg.click_policy
        target = pick_target(detections, policy)
        self._draw_preview(image, scale, target)
        if target is None:
            # 왜 못 찾았는지 근거를 남긴다. 추측하지 않게 하는 게 목적.
            self._log(f"한 번 검사: 못 찾음. {stats.summary()}")
            if stats.color_pixels == 0:
                self._log(
                    "  지정한 색이 영역 안에 아예 없습니다. 영역이 맞는지 미리보기로 "
                    "확인하고, 캘리브레이션으로 색을 다시 학습하세요."
                )
            for line in stats.top_rejects():
                self._log(f"  {line}")
            if stats.blobs and not stats.rejected:
                self._log("  색은 찾았지만 덩어리가 조건에 닿지 못했습니다.")
            # 색 기준을 무시한 자동 탐지로는 무엇이 보이는지도 알려준다.
            auto = detect_auto(image, self.cfg.detector)
            if auto:
                self._log(
                    f"  참고: 자동 탐지로는 후보 {len(auto)}개가 보입니다 "
                    "(시작하면 자동 캘리브레이션이 이 중에서 학습합니다)."
                )
                for candidate in auto[:3]:
                    self._log(
                        f"    후보 {candidate.width}x{candidate.height} "
                        f"색상 {candidate.hue:.0f}° 채움 {candidate.fill:.2f} "
                        f"글자 {candidate.text_ratio:.2f} 위치 "
                        f"({candidate.x}, {candidate.y})"
                    )
                self._draw_preview(image, scale, auto[0])
        else:
            cx, cy = target.center
            match_note = (
                f" 견본일치 {target.match:.2f}" if target.match > -2.0 else ""
            )
            self._log(
                f"한 번 검사: {target.width}x{target.height} "
                f"채움 {target.fill:.2f} 글자 {target.text_ratio:.2f}{match_note} -> "
                f"화면 좌표 ({int(self.region.x + cx / scale)}, "
                f"{int(self.region.y + cy / scale)})"
                + (f" / 후보 {len(detections)}개" if len(detections) > 1 else "")
            )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.region is None:
            messagebox.showwarning("영역 필요", "먼저 감시 영역을 선택하세요.")
            return
        if not self._collect():
            return
        if not self.cfg.dry_run and self.adapter.name == "unsupported":
            messagebox.showerror(
                "지원하지 않는 OS", "이 OS 에서는 클릭을 지원하지 않습니다. 테스트 모드만 가능합니다."
            )
            return
        if self._window_overlaps_region():
            self._log("경고: 이 창이 감시 영역과 겹칩니다. 창을 옆으로 옮기세요.")

        problems = self.adapter.check_permissions()
        if problems:
            body = "\n\n".join(f"- {p}" for p in problems)
            if not messagebox.askokcancel(
                "권한 부족", f"{body}\n\n그래도 시작할까요?", icon="warning"
            ):
                requester = getattr(self.adapter, "request_permissions", None)
                if callable(requester) and requester():
                    self._log("권한 요청 프롬프트를 표시했습니다. 허용 후 다시 시작하세요.")
                return

        save_config(self.config_dir, self.cfg)
        self.worker = ScanWorker(self.cfg, self.region, self.adapter, self.events)
        self.worker.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.test_btn.configure(state="disabled")
        self._set_settings_enabled(False)
        self.status_var.set("감시 중...")

    def _stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.stop_btn.configure(state="disabled")
            self.status_var.set("정지 중...")

    def _on_worker_stopped(self, reason: str) -> None:
        self.worker = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.test_btn.configure(state="normal")
        self._set_settings_enabled(True)
        self.status_var.set("정지")
        self._log(f"감시를 멈췄습니다 ({reason}).")

    def _window_overlaps_region(self) -> bool:
        if self.region is None:
            return False
        self.root.update_idletasks()
        wx, wy = self.root.winfo_rootx(), self.root.winfo_rooty()
        ww, wh = self.root.winfo_width(), self.root.winfo_height()
        return not (
            wx > self.region.right
            or wx + ww < self.region.x
            or wy > self.region.bottom
            or wy + wh < self.region.y
        )

    # ------------------------------------------------------------ 미리보기/큐
    def _draw_preview(self, image: np.ndarray, scale: float, target) -> None:
        small, step = downscale(image, self.preview_w, self.preview_h)
        try:
            photo = numpy_to_photo(small)
        except tk.TclError:
            return
        self._photo = photo
        canvas = self.preview
        canvas.delete("all")
        off_x = max(0, (self.preview_w - small.shape[1]) // 2)
        off_y = max(0, (self.preview_h - small.shape[0]) // 2)
        canvas.create_image(off_x, off_y, image=photo, anchor="nw")
        if target is not None:
            x0 = off_x + target.x / step
            y0 = off_y + target.y / step
            canvas.create_rectangle(
                x0,
                y0,
                x0 + target.width / step,
                y0 + target.height / step,
                outline="#00e676",
                width=2,
            )

    def _pump(self) -> None:
        latest_preview = None
        try:
            while True:
                event = self.events.get_nowait()
                if event.kind == "preview":
                    latest_preview = event.payload  # 마지막 것만 그린다
                elif event.kind == "log":
                    self._log(str(event.payload))
                elif event.kind == "status":
                    self.status_var.set(str(event.payload))
                elif event.kind == "click":
                    self.clicks_var.set(f"클릭 {event.payload}회")
                elif event.kind == "error":
                    self._log(f"오류: {event.payload}")
                elif event.kind == "offset":
                    # 워커가 학습한 클릭 보정값을 설정에 반영해 다음 실행에도 쓴다
                    self.cfg.click_offset_x, self.cfg.click_offset_y = event.payload
                    self._update_offset_label()
                elif event.kind == "calibrated":
                    # 워커가 스스로 학습한 인식 기준을 UI 에 반영
                    self.cfg.detector = event.payload.config
                    self._detector_to_ui()
                elif event.kind == "display_changed":
                    self._log(
                        "디스플레이 구성이 바뀌었습니다. 좌표가 어긋날 수 있어 "
                        "감시를 멈춥니다. 모니터 새로고침 후 영역을 확인하세요."
                    )
                    self.root.after(100, self._refresh_monitors)
                elif event.kind == "stopped":
                    self._on_worker_stopped(str(event.payload))
        except queue.Empty:
            pass
        if latest_preview is not None:
            self._draw_preview(*latest_preview)
        self.root.after(80, self._pump)

    # ----------------------------------------------------------------- 종료
    def _on_close(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=2.0)
        # 마지막 상태를 저장한다 (입력값이 잘못돼 있어도 나머지는 남긴다)
        self._collect(silent=True)
        try:
            save_config(self.config_dir, self.cfg)
        except OSError:
            pass
        self.capture.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hsv_to_rgb(hue: float, sat: float, val: float) -> tuple[int, int, int]:
    hue = hue % 360.0
    chroma = val * sat
    second = chroma * (1 - abs((hue / 60.0) % 2 - 1))
    match int(hue // 60) % 6:
        case 0:
            rgb = (chroma, second, 0.0)
        case 1:
            rgb = (second, chroma, 0.0)
        case 2:
            rgb = (0.0, chroma, second)
        case 3:
            rgb = (0.0, second, chroma)
        case 4:
            rgb = (second, 0.0, chroma)
        case _:
            rgb = (chroma, 0.0, second)
    base = val - chroma
    return tuple(int(round((c + base) * 255)) for c in rgb)  # type: ignore[return-value]


def main() -> None:
    AllowClickerApp().run()
