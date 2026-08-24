"""좌표/영역 표현.

여기서 다루는 좌표는 모두 '논리 좌표'(logical/point 단위)다.
- Windows: per-monitor DPI awareness를 켜므로 논리 좌표 == 물리 픽셀
- macOS: Retina 에서 논리 좌표 != 물리 픽셀 (캡처 이미지가 2배)
  -> 캡처 단계에서 scale 값을 함께 돌려주고, 클릭할 때 다시 논리 좌표로 환산한다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_points(cls, x0: int, y0: int, x1: int, y1: int) -> "Region":
        left, right = sorted((int(x0), int(x1)))
        top, bottom = sorted((int(y0), int(y1)))
        return cls(left, top, right - left, bottom - top)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def is_valid(self) -> bool:
        return self.width >= 8 and self.height >= 8

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Region | None":
        if not data:
            return None
        try:
            region = cls(
                int(data["x"]), int(data["y"]), int(data["width"]), int(data["height"])
            )
        except (KeyError, TypeError, ValueError):
            return None
        return region if region.is_valid else None

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return f"({self.x}, {self.y}) {self.width}x{self.height}"
