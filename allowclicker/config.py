"""설정 저장/불러오기 (JSON)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .detector import ButtonTemplate, DetectorConfig
from .geometry import Region

CONFIG_FILENAME = "config.json"


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
