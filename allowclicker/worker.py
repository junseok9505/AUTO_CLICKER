"""감시 스레드: 캡처 -> 탐지 -> 클릭.

안전 장치
- confirm_frames: 같은 위치에서 연속으로 감지되어야 클릭 (렌더링 중간 오클릭 방지)
- cooldown: 클릭 후 일정 시간 재클릭 금지
- armed: 한 번 클릭하면 버튼이 화면에서 사라진 뒤에만 다시 클릭 (연타 방지)
- dry_run: 감지 로그만 남기고 클릭하지 않음
- 전역 중지 핫키(F8) 감지
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .capture import ScreenCapture
from .config import AppConfig
from .detector import (
    MIN_AUTO_SCORE,
    Detection,
    calibrate_at,
    detect,
    detect_auto,
    pick_target,
)
from .geometry import Region
from .platforms.base import PlatformAdapter

PREVIEW_MIN_INTERVAL = 0.25  # 미리보기 전송 최소 간격(초)
DISPLAY_CHECK_INTERVAL = 3.0  # 디스플레이 배치 변경 확인 간격(초)
MAX_OFFSET = 200  # 자동 보정 한계(px). 이보다 커지면 설정이 잘못된 것이다.


@dataclass
class WorkerEvent:
    kind: str  # log | status | preview | click | error | stopped
    payload: Any = None


class ScanWorker(threading.Thread):
    def __init__(
        self,
        config: AppConfig,
        region: Region,
        adapter: PlatformAdapter,
        events: "queue.Queue[WorkerEvent]",
    ) -> None:
        super().__init__(name="allow-scan", daemon=True)
        self.config = config
        self.region = region
        self.adapter = adapter
        self.events = events
        self.stop_event = threading.Event()
        self.capture = ScreenCapture()
        self.click_count = 0
        # 학습된 클릭 좌표 보정값과 마지막 클릭 기록
        self.offset = [int(config.click_offset_x), int(config.click_offset_y)]
        self.last_click: dict | None = None
        self.calibrated = False
        self._weak_candidate_logged = False
        # 사용자가 지정한 버튼 견본이 있으면 그것이 최우선 기준이다.
        self.template = config.template
        self.policy = "match" if self.template is not None else config.click_policy

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------ 내부
    def _emit(self, kind: str, payload: Any = None) -> None:
        self.events.put(WorkerEvent(kind, payload))

    def _log(self, message: str) -> None:
        self._emit("log", message)

    def run(self) -> None:  # noqa: C901 - 루프 상태 관리라 한 곳에 두는 편이 읽기 쉽다
        cfg = self.config
        interval = max(0.05, float(cfg.interval))
        streak = 0
        last_center: tuple[float, float] | None = None
        clicked_center: tuple[float, float] | None = None  # 이번 버튼을 이미 눌렀는지
        retries_used = 0
        first_attempt_at = 0.0
        abandoned = False
        next_click_at = 0.0
        last_preview = 0.0
        warned_stuck = False
        reason = "사용자 중지"
        display_sig = self.adapter.display_signature()
        next_display_check = time.monotonic() + DISPLAY_CHECK_INTERVAL

        self._log(
            f"감시 시작: {self.region} / 간격 {interval:.2f}s"
            + ("  [테스트 모드: 클릭 안 함]" if cfg.dry_run else "")
        )
        if self.offset != [0, 0]:
            self._log(f"이전에 학습한 클릭 보정 적용: {tuple(self.offset)}")

        if self.template is not None:
            # 지정된 버튼 견본이 있으면 자동 학습 없이 그 기준으로 바로 감시한다.
            self.calibrated = True
            self._log(
                f"지정된 버튼 견본 사용: {self.template.describe()} "
                f"(모양 일치도 {cfg.detector.min_shape_match:.2f} 이상)"
            )
        # 시작할 때 스스로 캘리브레이션한다. 지금 화면에 버튼이 없으면
        # 자동 탐지 모드로 돌면서 버튼이 나타나는 순간 학습한다.
        elif cfg.auto_calibrate:
            try:
                image, _scale = self.capture.grab(self.region)
                if self._auto_calibrate(image):
                    self.calibrated = True
                else:
                    self._log(
                        "자동 캘리브레이션: 지금 영역에 버튼처럼 보이는 것이 없습니다. "
                        "자동 탐지 모드로 감시하다가 버튼이 나타나면 그때 학습합니다."
                    )
            except Exception as exc:
                self._emit("error", f"자동 캘리브레이션 실패: {exc}")
        else:
            self.calibrated = True  # 저장된 설정을 그대로 사용

        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                if self.adapter.stop_hotkey_pressed():
                    reason = f"{self.adapter.stop_hotkey_label} 키로 중지"
                    break

                # 감시 중 해상도/모니터 배치가 바뀌면 좌표가 무의미해진다.
                if started >= next_display_check:
                    next_display_check = started + DISPLAY_CHECK_INTERVAL
                    current_sig = self.adapter.display_signature()
                    if display_sig is not None and current_sig != display_sig:
                        self._emit("display_changed", current_sig)
                        reason = "디스플레이 구성 변경 감지 (영역 재설정 필요)"
                        break

                image, scale = self.capture.grab(self.region)
                if not self.calibrated:
                    # 아직 학습 못 했으면 색 무관 자동 탐지로 버튼을 기다린다.
                    if self._auto_calibrate(image):
                        self.calibrated = True
                    else:
                        self._emit("status", "자동 탐지 중 (버튼 대기)")
                detections = (
                    detect(image, cfg.detector, template=self.template)
                    if self.calibrated
                    else []
                )
                target = pick_target(detections, self.policy)

                if target is None:
                    if streak:
                        self._emit("status", "대기 중")
                    streak = 0
                    last_center = None
                    clicked_center = None  # 버튼이 사라졌으니 다음 버튼을 위해 초기화
                    retries_used = 0
                    first_attempt_at = 0.0
                    abandoned = False
                    warned_stuck = False
                else:
                    center = target.center
                    if last_center and _close(center, last_center, 6.0):
                        streak += 1
                    else:
                        streak = 1
                        warned_stuck = False
                    last_center = center

                now = time.monotonic()
                if self._should_preview(now, last_preview, target):
                    self._emit("preview", (image, scale, target))
                    last_preview = now

                if target is not None:
                    self._emit(
                        "status",
                        f"감지: {target.width}x{target.height} "
                        + (
                            f"견본일치 {target.match:.2f} "
                            if target.match > -2.0
                            else f"채움 {target.fill:.2f} "
                        )
                        + f"({streak}/{cfg.confirm_frames})",
                    )

                    ready = streak >= max(1, cfg.confirm_frames)
                    same_button = clicked_center is not None and _close(
                        center, clicked_center, 8.0
                    )
                    if not same_button:
                        # 다른 버튼(또는 위치 이동) -> 시도 이력 초기화
                        retries_used = 0
                        first_attempt_at = 0.0
                        abandoned = False
                        warned_stuck = False

                    if ready and now >= next_click_at and not abandoned:
                        if not same_button:
                            # 새 버튼: 첫 클릭
                            self._handle_hit(target, scale, cfg)
                            clicked_center = center
                            first_attempt_at = now
                            next_click_at = now + max(0.1, float(cfg.cooldown))
                            streak = 0
                            last_center = None
                        else:
                            # 눌렀는데도 그대로 있다.
                            # 커서가 실제로 착지한 좌표와 지금 보이는 버튼 위치를
                            # 비교해서 스스로 보정한 뒤 다시 누른다.
                            limit = max(0, int(cfg.max_retries))  # 0 = 사라질 때까지
                            timeout = max(1.0, float(cfg.retry_timeout))
                            if now - first_attempt_at > timeout:
                                abandoned = True
                                # 잘못 학습했을 가능성이 있으니 다시 학습하게 한다.
                                if cfg.auto_calibrate:
                                    self.calibrated = False
                                self._log(
                                    f"{timeout:.0f}초 동안 눌러도 버튼이 사라지지 않아 "
                                    "이 버튼은 건너뜁니다. 창이 가려졌는지, 미리보기의 "
                                    "초록 사각형이 실제 버튼과 맞는지 확인하세요."
                                )
                            elif limit and retries_used >= limit:
                                abandoned = True
                                self._log(
                                    f"재시도 {limit}회를 모두 사용했습니다. "
                                    "이 버튼은 건너뜁니다."
                                )
                            else:
                                retries_used += 1
                                self._log(
                                    "버튼이 남아 있어 재시도합니다 "
                                    f"({retries_used}"
                                    + (f"/{limit}" if limit else "")
                                    + ")"
                                )
                                self._handle_hit(target, scale, cfg)
                                next_click_at = now + max(0.1, float(cfg.cooldown))
                                streak = 0
                                last_center = None
                        if cfg.max_clicks and self.click_count >= cfg.max_clicks:
                            reason = f"최대 클릭 횟수({cfg.max_clicks}) 도달"
                            break
                    elif abandoned and not warned_stuck:
                        warned_stuck = True
                        self._emit("status", "건너뜀 (버튼이 사라지지 않음)")
            except Exception as exc:  # 캡처 실패 등: 죽지 않고 계속
                self._emit("error", f"{type(exc).__name__}: {exc}")
                time.sleep(0.5)

            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.0, interval - elapsed))

        self.capture.close()
        self._emit("stopped", reason)

    def _should_preview(
        self, now: float, last_preview: float, target: Detection | None
    ) -> bool:
        if now - last_preview >= PREVIEW_MIN_INTERVAL:
            return True
        return target is not None and now - last_preview >= 0.1

    def _auto_calibrate(self, image: np.ndarray) -> bool:
        """영역에서 버튼처럼 보이는 것을 스스로 찾아 인식 기준을 학습한다."""
        candidates = detect_auto(image, self.config.detector)
        if not candidates:
            return False
        best = candidates[0]
        if best.score < MIN_AUTO_SCORE:
            # 버튼이라고 확신하기 어려운 것(긴 막대, 글자 없는 색 블록 등)에
            # 잘못 학습하면 엉뚱한 곳을 클릭한다. 더 나은 후보를 기다린다.
            if not self._weak_candidate_logged:
                self._weak_candidate_logged = True
                self._log(
                    f"자동 캘리브레이션: 후보({best.width}x{best.height}, "
                    f"색상 {best.hue:.0f}°, 점수 {best.score:.2f})가 버튼이라고 "
                    "보기 어려워 학습을 보류합니다. 영역을 버튼 주변으로 좁히면 "
                    "정확해집니다."
                )
            return False
        cx, cy = best.center
        result = calibrate_at(image, (int(cx), int(cy)), self.config.detector)
        if result is None:
            return False

        # 학습한 기준으로 실제로 다시 찾히는지 확인한다 (헛학습 방지).
        verify = detect(image, result.config)
        if not verify:
            self._log(
                f"자동 캘리브레이션 후보({best.width}x{best.height}, "
                f"색상 {best.hue:.0f}°)를 검증하지 못해 건너뜁니다."
            )
            return False

        self.config.detector = result.config
        self._log(
            f"자동 캘리브레이션 완료: {result.describe()}"
            + (f" / 후보 {len(candidates)}개 중 선택" if len(candidates) > 1 else "")
        )
        for warning in result.warnings:
            self._log(f"  주의: {warning}")
        self._emit("calibrated", result)
        return True

    def _to_screen(self, cx: float, cy: float, scale: float) -> tuple[int, int]:
        """캡처 이미지 좌표 -> 화면 좌표 (학습된 보정 포함)."""
        factor = max(scale, 1e-6)
        return (
            int(round(self.region.x + cx / factor + self.offset[0])),
            int(round(self.region.y + cy / factor + self.offset[1])),
        )

    def _apply_offset(self, dx: int, dy: int, why: str) -> bool:
        if dx == 0 and dy == 0:
            return False
        new_x = _clamp_int(self.offset[0] + dx, -MAX_OFFSET, MAX_OFFSET)
        new_y = _clamp_int(self.offset[1] + dy, -MAX_OFFSET, MAX_OFFSET)
        if (new_x, new_y) == (self.offset[0], self.offset[1]):
            return False
        self.offset = [new_x, new_y]
        self._log(f"좌표 자동 보정: {why} -> 누적 보정 ({new_x:+d}, {new_y:+d})")
        self._emit("offset", (new_x, new_y))
        return True

    def _handle_hit(self, target: Detection, scale: float, cfg: AppConfig) -> None:
        """버튼을 클릭한다.

        누르기 전에 '커서가 실제로 어디에 있는지' 확인하는 것이 핵심이다.
        요청한 좌표와 실제 좌표가 다르면(배율/좌표계 문제) 그 차이를 학습해서
        다시 이동한 뒤에 누른다. 그래서 첫 클릭부터 제대로 들어간다.
        """
        cx, cy = target.center
        screen_x, screen_y = self._to_screen(cx, cy, scale)

        if cfg.dry_run:
            self._log(f"[테스트] 클릭 대상 좌표 ({screen_x}, {screen_y}) - 실제 클릭 안 함")
            self.last_click = {"requested": (screen_x, screen_y), "actual": None}
            return

        origin = None
        if cfg.restore_cursor:
            try:
                origin = self.adapter.cursor_position()
            except Exception:
                origin = None

        if cfg.activate_before_click:
            # 비활성 창은 첫 클릭이 활성화에만 쓰이는 경우가 있어 미리 올려둔다.
            try:
                if self.adapter.activate_window_at(screen_x, screen_y):
                    time.sleep(0.08)
            except Exception:
                pass

        actual = self._move_and_verify(screen_x, screen_y)
        # 좌표가 어긋났으면 보정하고 한 번만 다시 맞춘다.
        if (
            cfg.auto_offset
            and actual is not None
            and (abs(actual[0] - screen_x) > 1 or abs(actual[1] - screen_y) > 1)
        ):
            dx, dy = screen_x - actual[0], screen_y - actual[1]
            if self._apply_offset(
                dx, dy, f"커서가 {(screen_x, screen_y)} 대신 {actual} 에 도착"
            ):
                screen_x, screen_y = self._to_screen(cx, cy, scale)
                actual = self._move_and_verify(screen_x, screen_y)

        time.sleep(0.03)  # hover 반영 시간
        self.adapter.press_left()
        self.last_click = {"requested": (screen_x, screen_y), "actual": actual}

        self.click_count += 1
        where = actual if actual is not None else (screen_x, screen_y)
        self._log(f"클릭! {tuple(where)}  누적 {self.click_count}회")
        self._emit("click", self.click_count)

        if origin:
            time.sleep(0.05)
            try:
                self.adapter.move_cursor(*origin)
            except Exception:
                pass

    def _move_and_verify(self, x: int, y: int) -> tuple[int, int] | None:
        """커서를 옮기고, 실제로 도착한 좌표를 확인해서 돌려준다."""
        try:
            self.adapter.move_cursor(x, y)
            return self.adapter.wait_for_cursor(x, y)
        except Exception as exc:
            self._emit("error", f"커서 이동 실패: {exc}")
            return None


def _close(a: tuple[float, float], b: tuple[float, float], tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def downscale(image: np.ndarray, max_width: int, max_height: int) -> tuple[np.ndarray, int]:
    """미리보기용 정수배 축소. (축소 이미지, step) 반환."""
    height, width = image.shape[:2]
    step = 1
    while (width // step > max_width or height // step > max_height) and step < 8:
        step += 1
    return image[::step, ::step], step
