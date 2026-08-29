"""설정 저장/불러오기 (JSON)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .detector import ButtonTemplate, DetectorConfig
from .geometry import Region

CONFIG_FILENAME = "config.json"

# 예전 기본값들. 저장된 값이 예전 기본값과 정확히 같으면 사용자가 손댄 적이 없다고
# 보고 새 기본값으로 올린다. 0.28/0.25 는 대화 본문의 인라인 코드 배경(#342F44,
# 채도 0.31/명도 0.27) 같은 '옅게 깔린 같은 색'까지 버튼 후보로 통과시켜서,
# 화면 곳곳의 어두운 보라 덩어리를 버튼으로 오인하게 만든다.
_OUTDATED_DEFAULTS = {"sat_min": 0.28, "val_min": 0.25}


@dataclass
class AppConfig:
    region: Region | None = None  # 감시 영역
    button_rect: Region | None = None  # 사용자가 지정한 '눌러야 하는 버튼' 영역
    template: ButtonTemplate | None = None  # 그 버튼의 견본 이미지
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    interval: float = 0.4  # 화면 검사 주기(초)
    cooldown: float = 1.5  # 클릭 후 재클릭 금지 시간(초)
    confirm_frames: int = 2  # 연속 감지 횟수 (렌더링 도중 오클릭 방지)
    dry_run: bool = False  # 켜면 감지만 하고 클릭하지 않음
    restore_cursor: bool = True  # 클릭 후 마우스 원위치 복귀
    click_policy: str = "leftmost"  # leftmost | score
    max_clicks: int = 0  # 0 = 무제한
    monitor_index: int = 0  # 영역 선택에 사용할 모니터 (0 = 전체)
    activate_before_click: bool = True  # 클릭 전 대상 창 활성화
    max_retries: int = 0  # 버튼이 남아 있을 때 재시도 (0 = 사라질 때까지)
    retry_timeout: float = 20.0  # 한 버튼에 매달릴 최대 시간(초)
    auto_calibrate: bool = True  # 시작할 때 스스로 인식 기준 학습
    auto_offset: bool = True  # 클릭 좌표 자동 보정
    click_offset_x: int = 0  # 학습된 보정값
    click_offset_y: int = 0
    notes: list[str] = field(default_factory=list)  # 불러올 때 생긴 안내 (저장 안 함)

    def to_dict(self) -> dict:
        return {
            "region": self.region.to_dict() if self.region else None,
            "button_rect": self.button_rect.to_dict() if self.button_rect else None,
            "template": self.template.to_dict() if self.template else None,
            "detector": self.detector.to_dict(),
            "interval": self.interval,
            "cooldown": self.cooldown,
            "confirm_frames": self.confirm_frames,
            "dry_run": self.dry_run,
            "restore_cursor": self.restore_cursor,
            "click_policy": self.click_policy,
            "max_clicks": self.max_clicks,
            "monitor_index": self.monitor_index,
            "activate_before_click": self.activate_before_click,
            "max_retries": self.max_retries,
            "retry_timeout": self.retry_timeout,
            "auto_calibrate": self.auto_calibrate,
            "auto_offset": self.auto_offset,
            "click_offset_x": self.click_offset_x,
            "click_offset_y": self.click_offset_y,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppConfig":
        cfg = cls()
        if not data:
            return cfg
        cfg.region = Region.from_dict(data.get("region"))
        cfg.button_rect = Region.from_dict(data.get("button_rect"))
        cfg.template = ButtonTemplate.from_dict(data.get("template"))
        cfg.detector = DetectorConfig.from_dict(data.get("detector"))
        cfg.notes = _upgrade_detector(cfg.detector, data.get("detector"))
        for key in (
            "interval",
            "cooldown",
            "confirm_frames",
            "dry_run",
            "restore_cursor",
            "click_policy",
            "max_clicks",
            "monitor_index",
            "activate_before_click",
            "max_retries",
            "retry_timeout",
            "auto_calibrate",
            "auto_offset",
            "click_offset_x",
            "click_offset_y",
        ):
            if data.get(key) is not None:
                current = getattr(cfg, key)
                try:
                    setattr(cfg, key, type(current)(data[key]))
                except (TypeError, ValueError):
                    pass
        return cfg


def _upgrade_detector(detector: DetectorConfig, stored: dict | None) -> list[str]:
    """예전 기본값이 저장돼 있으면 새 기본값으로 올린다.

    설정 파일에는 모든 항목이 그대로 저장되기 때문에, 기본값이 바뀌어도 예전에
    저장한 파일을 쓰는 동안에는 옛 값이 계속 살아 있다. 사용자가 직접 고친 값은
    건드리지 않고, '한 번도 손대지 않은 예전 기본값'만 올린다.
    """
    if not stored:
        return []
    fresh = DetectorConfig()
    notes: list[str] = []
    for key, old_default in _OUTDATED_DEFAULTS.items():
        new_default = getattr(fresh, key)
        if abs(float(stored.get(key, new_default)) - old_default) < 1e-9:
            setattr(detector, key, new_default)
            notes.append(f"{key} {old_default:g} -> {new_default:g}")
    if notes:
        return [
            "인식 기준을 새 기본값으로 올렸습니다 (" + ", ".join(notes) + "). "
            "예전 값은 대화 본문의 옅은 보라 배경까지 버튼으로 인식했습니다."
        ]
    return []


def config_path(config_dir: Path) -> Path:
    return config_dir / CONFIG_FILENAME


def load_config(config_dir: Path) -> AppConfig:
    path = config_path(config_dir)
    if not path.exists():
        return AppConfig()
    try:
        with path.open("r", encoding="utf-8") as fp:
            return AppConfig.from_dict(json.load(fp))
    except (OSError, json.JSONDecodeError):
        return AppConfig()


def save_config(config_dir: Path, config: AppConfig) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_path(config_dir)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(config.to_dict(), fp, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path
