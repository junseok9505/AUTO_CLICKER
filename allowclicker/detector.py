"""보라색 버튼 탐지기 (numpy 만 사용, OS 무관).

동작 원리
1. RGB -> HSV 변환 후 목표 색상(hue) 근처의 채도/명도 조건을 만족하는 픽셀 마스크 생성
2. 마스크의 연결 요소(run-length + union-find)로 후보 덩어리 추출
3. 크기 / 가로세로비 / 채움율(fill) / 내부 밝은 글자 비율로 '버튼'만 남김

채움율 조건이 중요하다. Kiro 대화상자의 보라색 '테두리'는 같은 색이지만
속이 비어 있어 채움율이 낮으므로 자동으로 걸러진다.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field, replace

import numpy as np

# 연결 요소 계산 폭주 방지용 상한
_MAX_RUNS = 60000


@dataclass
class DetectorConfig:
    """탐지 파라미터.

    기본값은 '보라색 알약형 버튼' 일반 조건이다. 실제 화면에서 안 잡히면
    캘리브레이션(버튼을 직접 클릭)으로 이 값들을 측정값 기반으로 덮어쓴다.
    """

    hue_center: float = 258.0  # 보라색 (#8B5CF6 계열)
    hue_tolerance: float = 30.0
    # 채도/명도 하한은 '색이 꽉 찬 강조색 버튼'과 '같은 색을 옅게 깐 배경'을
    # 가르는 선이다. 실측: Kiro 강조색 버튼 #7454DE = 채도 0.62 / 명도 0.87,
    # 대화 본문의 인라인 코드 배경 #342F44 = 채도 0.31 / 명도 0.27.
    # 하한이 낮으면 코드 배경 같은 어두운 보라 덩어리가 전부 버튼 후보가 된다.
    sat_min: float = 0.45
    val_min: float = 0.55
    min_width: int = 30
    max_width: int = 600
    min_height: int = 14
    max_height: int = 120
    min_aspect: float = 1.2
    max_aspect: float = 12.0
    min_fill: float = 0.50
    require_text: bool = True
    min_text_ratio: float = 0.010
    max_text_ratio: float = 0.50
    text_val_min: float = 0.70
    text_sat_max: float = 0.45
    # 내부 밝은 픽셀의 가로 무게중심이 중앙에서 얼마나 벗어나도 되는지 (폭 대비 비율).
    # 글자 라벨은 가운데 정렬이라 편차가 거의 0인데, 토글 스위치의 흰 손잡이는
    # 한쪽으로 몰려 있다. 실측: 글자 버튼 0.02 이하, Autopilot 토글 0.21.
    max_label_offset: float = 0.15
    # 버튼 견본(템플릿)이 있을 때 요구하는 모양 일치도 (-1~1). 밝기 변화에 강한 ZNCC.
    min_shape_match: float = 0.45

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "DetectorConfig":
        cfg = cls()
        if not data:
            return cfg
        for key, value in data.items():
            if hasattr(cfg, key) and value is not None:
                current = getattr(cfg, key)
                try:
                    setattr(cfg, key, type(current)(value))
                except (TypeError, ValueError):
                    pass
        return cfg


@dataclass(frozen=True)
class Detection:
    """탐지 결과. 좌표는 캡처 이미지의 픽셀 좌표."""

    x: int
    y: int
    width: int
    height: int
    fill: float
    text_ratio: float
    score: float
    hue: float = -1.0  # 자동 탐지에서 추정한 색상(°). -1 = 미측정
    match: float = -2.0  # 견본과의 모양 일치도. -2 = 견본 없음

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2.0, self.y + self.height / 2.0


def rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """RGB uint8 (H,W,3) -> (hue 0~360, sat 0~1, val 0~1)."""
    arr = rgb.astype(np.float32) / 255.0
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    mx = arr.max(axis=-1)
    mn = arr.min(axis=-1)
    diff = mx - mn

    hue = np.zeros_like(mx)
    nonzero = diff > 1e-6
    is_r = nonzero & (mx == r)
    is_g = nonzero & (mx == g) & ~is_r
    is_b = nonzero & (mx == b) & ~is_r & ~is_g

    with np.errstate(divide="ignore", invalid="ignore"):
        hue[is_r] = ((g[is_r] - b[is_r]) / diff[is_r]) % 6.0
        hue[is_g] = ((b[is_g] - r[is_g]) / diff[is_g]) + 2.0
        hue[is_b] = ((r[is_b] - g[is_b]) / diff[is_b]) + 4.0
    hue *= 60.0

    sat = np.where(mx > 1e-6, diff / np.maximum(mx, 1e-6), 0.0)
    return hue, sat, mx


def hue_distance(hue: np.ndarray, center: float) -> np.ndarray:
    """색상환에서의 최단 거리(0~180)."""
    return np.abs(((hue - center + 180.0) % 360.0) - 180.0)


def label_offset(bright: np.ndarray) -> float:
    """덩어리 안 밝은 픽셀의 가로 무게중심이 중앙에서 벗어난 정도 (폭 대비 0~0.5).

    글자 라벨은 가운데 정렬이라 이 값이 0에 가깝다. 반면 토글 스위치의 흰 손잡이는
    한쪽 끝에 몰려 있어 0.2 이상 나온다. 스위치와 버튼은 색·크기·채움율이 사실상
    같아서 구분할 방법이 없는데, 이 값으로는 갈린다.
    """
    total = float(bright.sum())
    width = bright.shape[1] if bright.ndim == 2 else 0
    if total <= 0.0 or width < 2:
        return 0.0
    centroid = float(bright.sum(axis=0).astype(np.float64) @ np.arange(width)) / total
    return abs(centroid - (width - 1) / 2.0) / float(width)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: list[int] = []

    def add(self) -> int:
        idx = len(self.parent)
        self.parent.append(idx)
        return idx

    def find(self, i: int) -> int:
        parent = self.parent
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            self.parent[hi] = lo


def _row_runs(row: np.ndarray) -> np.ndarray:
    """한 행에서 True 구간을 [[start, end_exclusive], ...] 로 반환."""
    padded = np.concatenate(([0], row.astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    return edges.reshape(-1, 2)


def connected_components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """8-이웃 연결 요소를 (x0, y0, x1, y1, area) 목록으로 반환. x1/y1 은 exclusive."""
    height = mask.shape[0]
    uf = _UnionFind()
    runs: list[tuple[int, int, int]] = []  # (y, x0, x1)
    prev: list[tuple[int, int, int]] = []  # (index, x0, x1)

    for y in range(height):
        current: list[tuple[int, int, int]] = []
        for x0, x1 in _row_runs(mask[y]):
            idx = uf.add()
            runs.append((y, int(x0), int(x1)))
            for pidx, px0, px1 in prev:
                # 대각선까지 이어붙이기 위해 1픽셀 여유를 둔다.
                if x0 - 1 < px1 and px0 < x1 + 1:
                    uf.union(idx, pidx)
            current.append((idx, int(x0), int(x1)))
        prev = current
        if len(runs) > _MAX_RUNS:  # 노이즈가 심하면 포기 (오탐/과부하 방지)
            return []

    boxes: dict[int, list[int]] = {}
    for idx, (y, x0, x1) in enumerate(runs):
        root = uf.find(idx)
        box = boxes.get(root)
        if box is None:
            boxes[root] = [x0, y, x1, y + 1, x1 - x0]
        else:
            box[0] = min(box[0], x0)
            box[1] = min(box[1], y)
            box[2] = max(box[2], x1)
            box[3] = max(box[3], y + 1)
            box[4] += x1 - x0
    return [tuple(v) for v in boxes.values()]  # type: ignore[misc]


@dataclass
class DetectStats:
    """왜 못 찾았는지 설명하기 위한 진단 정보."""

    total_pixels: int = 0
    color_pixels: int = 0
    blobs: int = 0
    rejected: list[tuple[str, dict]] = field(default_factory=list)

    def summary(self) -> str:
        ratio = (self.color_pixels / self.total_pixels * 100) if self.total_pixels else 0
        return (
            f"픽셀 {self.total_pixels}개 중 지정 색 {self.color_pixels}개({ratio:.1f}%), "
            f"덩어리 {self.blobs}개"
        )

    def top_rejects(self, limit: int = 5) -> list[str]:
        # 안티에일리어싱으로 생긴 1~2픽셀 잡티는 진단에 도움이 안 되므로 뒤로 밀어둔다.
        meaningful = [r for r in self.rejected if r[1]["w"] >= 6 or r[1]["h"] >= 6]
        chosen = meaningful[:limit] or self.rejected[:2]
        lines = []
        for reason, info in chosen:
            lines.append(
                f"제외({reason}): {info['w']}x{info['h']} "
                f"채움 {info['fill']:.2f} 글자 {info.get('text_ratio', -1):.2f} "
                f"위치 ({info['x']}, {info['y']})"
            )
        return lines


THUMB_WIDTH = 40
THUMB_HEIGHT = 16


def _resize_nearest(image: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    """의존성 없이 최근접 이웃으로 크기 변경."""
    height, width = image.shape[:2]
    ys = np.clip((np.arange(out_h) * height) // max(1, out_h), 0, height - 1)
    xs = np.clip((np.arange(out_w) * width) // max(1, out_w), 0, width - 1)
    return image[ys][:, xs]


@dataclass
class ButtonTemplate:
    """사용자가 지정한 '눌러야 하는 버튼'의 견본.

    원래 크기와, 크기를 정규화한 작은 썸네일을 갖고 있다. 썸네일을 밝기에 둔감한
    방식(ZNCC)으로 비교하므로, hover 로 색이 조금 밝아져도 같은 버튼으로 인식하고
    글자 모양이 다른 버튼('Always allow' 등)은 걸러낸다.
    """

    width: int
    height: int
    thumb: np.ndarray  # (THUMB_HEIGHT, THUMB_WIDTH, 3) uint8

    def to_dict(self) -> dict:
        return {
            "width": int(self.width),
            "height": int(self.height),
            "thumb_w": int(self.thumb.shape[1]),
            "thumb_h": int(self.thumb.shape[0]),
            "thumb": base64.b64encode(
                np.ascontiguousarray(self.thumb, dtype=np.uint8).tobytes()
            ).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ButtonTemplate | None":
        if not data:
            return None
        try:
            thumb_w = int(data["thumb_w"])
            thumb_h = int(data["thumb_h"])
            raw = base64.b64decode(data["thumb"])
            if len(raw) != thumb_w * thumb_h * 3:
                return None
            thumb = np.frombuffer(raw, dtype=np.uint8).reshape(thumb_h, thumb_w, 3)
            return cls(int(data["width"]), int(data["height"]), thumb.copy())
        except (KeyError, TypeError, ValueError):
            return None

    def describe(self) -> str:
        return f"견본 {self.width}x{self.height}"


def make_template(crop: np.ndarray) -> ButtonTemplate | None:
    """버튼 영역 이미지로 견본을 만든다."""
    if crop.size == 0 or crop.shape[0] < 3 or crop.shape[1] < 3:
        return None
    thumb = _resize_nearest(crop, THUMB_WIDTH, THUMB_HEIGHT)
    return ButtonTemplate(
        width=int(crop.shape[1]), height=int(crop.shape[0]), thumb=thumb.copy()
    )


def trim_to_button(crop: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """지정한 영역에서 버튼 본체만 잘라낸다. (잘린 이미지, (x, y) 오프셋)

    사람이 드래그하면 버튼 밖 여백이 몇 픽셀 섞인다. 여백이 있으면 견본 안에서
    글자 위치가 밀려서, 탐지된 버튼(덩어리 경계 그대로)과 무늬가 어긋나
    일치도가 크게 떨어진다. 그래서 등록 시점에 버튼 경계로 맞춰준다.
    """
    reference = dominant_color(crop)
    if reference is None:
        return crop, (0, 0)
    hue, sat, val = rgb_to_hsv(crop)
    mask = _similar_mask(hue, sat, val, reference)
    blobs = connected_components(mask)
    if not blobs:
        return crop, (0, 0)
    # 픽셀 수가 가장 많은 덩어리가 버튼 본체다 (얇은 테두리선은 면적이 작다).
    x0, y0, x1, y1, _area = max(blobs, key=lambda blob: blob[4])
    if (x1 - x0) < 6 or (y1 - y0) < 4:
        return crop, (0, 0)
    return crop[y0:y1, x0:x1], (int(x0), int(y0))


def match_score(crop: np.ndarray, template: ButtonTemplate) -> float:
    """견본과의 일치도 (-1~1). 밝기/대비 변화에 둔감한 정규 상호상관."""
    if crop.size == 0:
        return -1.0
    thumb = _resize_nearest(crop, template.thumb.shape[1], template.thumb.shape[0])
    a = thumb.astype(np.float32).mean(axis=2)
    b = template.thumb.astype(np.float32).mean(axis=2)
    a = a - a.mean()
    b = b - b.mean()
    da = float(np.sqrt((a * a).mean()))
    db = float(np.sqrt((b * b).mean()))
    if da < 1e-6 or db < 1e-6:
        # 둘 중 하나가 완전 단색이면 무늬로 비교할 수 없다.
        return 1.0 if da < 1e-6 and db < 1e-6 else 0.0
    return float(np.clip((a * b).mean() / (da * db), -1.0, 1.0))


def component_at(
    mask: np.ndarray, px: int, py: int
) -> tuple[int, int, int, int, int] | None:
    """(px, py) 픽셀이 실제로 속한 연결 요소만 찾아 (x0, y0, x1, y1, area) 반환.

    bbox 포함 관계로 찾으면 '속이 빈 큰 테두리'의 bbox 안에 있는 버튼을
    테두리로 오인한다. 그래서 클릭 지점에서 직접 퍼져 나가며(flood fill) 찾는다.
    """
    height, width = mask.shape
    if not (0 <= px < width and 0 <= py < height) or not mask[py, px]:
        return None

    runs_by_row = [_row_runs(mask[y]) for y in range(height)]
    start = None
    for index, (x0, x1) in enumerate(runs_by_row[py]):
        if x0 <= px < x1:
            start = (py, index)
            break
    if start is None:
        return None

    visited: set[tuple[int, int]] = {start}
    stack = [start]
    min_x, min_y = width, height
    max_x, max_y = 0, 0
    area = 0
    while stack:
        row, index = stack.pop()
        x0, x1 = (int(v) for v in runs_by_row[row][index])
        area += x1 - x0
        min_x, max_x = min(min_x, x0), max(max_x, x1)
        min_y, max_y = min(min_y, row), max(max_y, row + 1)
        for neighbor in (row - 1, row + 1):
            if not (0 <= neighbor < height):
                continue
            for other, (ox0, ox1) in enumerate(runs_by_row[neighbor]):
                # 8-이웃: 대각선으로 닿아도 같은 덩어리
                if x0 - 1 < ox1 and ox0 < x1 + 1 and (neighbor, other) not in visited:
                    visited.add((neighbor, other))
                    stack.append((neighbor, other))
    return min_x, min_y, max_x, max_y, area


def detect(
    rgb: np.ndarray,
    config: DetectorConfig,
    stats: DetectStats | None = None,
    template: ButtonTemplate | None = None,
) -> list[Detection]:
    """이미지에서 버튼 후보를 찾아 점수 내림차순으로 반환.

    stats 를 넘기면 탈락 이유를 기록한다(진단용).
    template 을 넘기면 모양까지 비교해서 다른 버튼을 걸러낸다.
    """
    if rgb.size == 0:
        return []

    hue, sat, val = rgb_to_hsv(rgb)
    purple = (
        (hue_distance(hue, config.hue_center) <= config.hue_tolerance)
        & (sat >= config.sat_min)
        & (val >= config.val_min)
    )
    if stats is not None:
        stats.total_pixels = int(purple.size)
        stats.color_pixels = int(purple.sum())
    if not purple.any():
        return []

    bright = (val >= config.text_val_min) & (sat <= config.text_sat_max)
    bright_int = bright.astype(np.int32)
    # 적분 이미지로 bbox 내부 밝은 픽셀 수를 빠르게 센다.
    integral = np.pad(bright_int.cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0)))

    blobs = connected_components(purple)
    if stats is not None:
        stats.blobs = len(blobs)
    # 큰 덩어리부터 살펴봐야 진단 로그가 쓸모 있다.
    blobs.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)

    results: list[Detection] = []
    for x0, y0, x1, y1, area in blobs:
        width, height = x1 - x0, y1 - y0
        fill = area / float(width * height)
        bright_count = int(
            integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
        )
        text_ratio = bright_count / float(width * height)
        info = {
            "x": int(x0),
            "y": int(y0),
            "w": int(width),
            "h": int(height),
            "fill": float(fill),
            "text_ratio": float(text_ratio),
        }

        def reject(reason: str) -> None:
            if stats is not None:
                stats.rejected.append((reason, info))

        if not (config.min_width <= width <= config.max_width):
            reject(f"가로 {width} 허용 {config.min_width}~{config.max_width}")
            continue
        if not (config.min_height <= height <= config.max_height):
            reject(f"세로 {height} 허용 {config.min_height}~{config.max_height}")
            continue
        aspect = width / float(height)
        if not (config.min_aspect <= aspect <= config.max_aspect):
            reject(f"비율 {aspect:.2f} 허용 {config.min_aspect}~{config.max_aspect}")
            continue
        if fill < config.min_fill:
            reject(f"채움율 {fill:.2f} < {config.min_fill} (테두리처럼 속이 빈 모양)")
            continue
        if config.require_text and not (
            config.min_text_ratio <= text_ratio <= config.max_text_ratio
        ):
            reject(
                f"글자비율 {text_ratio:.3f} 허용 "
                f"{config.min_text_ratio}~{config.max_text_ratio}"
            )
            continue
        offset = label_offset(bright[y0:y1, x0:x1]) if config.require_text else 0.0
        if config.require_text and offset > config.max_label_offset:
            # 글자 라벨이라면 가운데 정렬인데 한쪽으로 몰려 있다 -> 토글 스위치 등.
            reject(
                f"밝은 부분이 한쪽으로 쏠림 {offset:.2f} > "
                f"{config.max_label_offset:.2f} (글자 라벨이 아니라 스위치 손잡이 모양)"
            )
            continue

        match = -2.0
        if template is not None:
            match = match_score(rgb[y0:y1, x0:x1], template)
            info["match"] = match
            if match < config.min_shape_match:
                reject(
                    f"견본 일치도 {match:.2f} < {config.min_shape_match:.2f} "
                    "(다른 버튼으로 보임)"
                )
                continue

        text_score = min(text_ratio / 0.12, 1.0)
        score = fill * 0.6 + text_score * 0.4
        results.append(
            Detection(
                x=int(x0),
                y=int(y0),
                width=int(width),
                height=int(height),
                fill=round(float(fill), 3),
                text_ratio=round(float(text_ratio), 3),
                score=round(float(score), 3),
                match=round(float(match), 3),
            )
        )

    results.sort(key=lambda d: d.score, reverse=True)
    return results


HUE_BUCKETS = 24  # 15도 간격
PREFERRED_HUE = 265.0  # 보라색을 우선하되 다른 색도 후보로 본다
MAX_BUCKETS_SCANNED = 6
TYPICAL_TEXT_RATIO = 0.12  # 글자가 있는 버튼의 대표적인 밝은 픽셀 비율
MIN_AUTO_SCORE = 0.55  # 자동 학습을 신뢰할 최소 점수
# 1등과 2등 점수 차가 이보다 작으면 '어느 것이 그 버튼인지' 판단할 수 없다고 본다.
AMBIGUOUS_SCORE_GAP = 0.06


def button_likeness(
    width: int,
    height: int,
    fill: float,
    text_ratio: float,
    hue: float,
    offset: float = 0.0,
) -> float:
    """'버튼답기' 점수 (0~1).

    긴 막대(강조 표시줄), 큰 패널, 글자 없는 색 블록보다
    '적당한 크기 + 적당한 비율 + 내부에 가운데 정렬된 글자' 인 것을 높게 본다.
    보라색이면 가점을 주되, 다른 색 버튼도 후보로 남긴다.
    offset 은 내부 밝은 픽셀의 가로 쏠림(label_offset)이다.
    """
    aspect = width / float(max(1, height))
    if 1.5 <= aspect <= 5.0:
        aspect_score = 1.0
    else:
        gap = 1.5 - aspect if aspect < 1.5 else aspect - 5.0
        aspect_score = max(0.0, 1.0 - gap / 5.0)

    text_score = max(
        0.0, 1.0 - abs(text_ratio - TYPICAL_TEXT_RATIO) / TYPICAL_TEXT_RATIO
    )
    size_score = 1.0 if width <= 250 else max(0.0, 1.0 - (width - 250) / 400.0)
    purple_score = 1.0 - min(
        1.0, float(hue_distance(np.array([float(hue)]), PREFERRED_HUE)[0]) / 90.0
    )
    # 라벨이 가운데 있을수록 버튼답다 (한쪽으로 몰려 있으면 스위치 손잡이).
    label_score = max(0.0, 1.0 - offset / 0.15)
    return round(
        purple_score * 0.25
        + text_score * 0.20
        + label_score * 0.15
        + aspect_score * 0.15
        + fill * 0.15
        + size_score * 0.10,
        3,
    )


def detect_auto(
    rgb: np.ndarray, config: DetectorConfig, stats: DetectStats | None = None
) -> list[Detection]:
    """색을 미리 정하지 않고 '버튼처럼 생긴 것'을 찾는다 (자동 캘리브레이션용).

    색상을 15도 구간으로 나눠 각 구간마다 따로 덩어리를 찾는다. 그래야 서로 다른
    색의 이웃한 UI 요소가 한 덩어리로 붙지 않는다. 크기/비율/채움율/내부 밝은 글자
    조건을 통과한 것만 후보로 두고, 보라색에 가까울수록 점수를 높게 준다.
    """
    if rgb.size == 0:
        return []

    hue, sat, val = rgb_to_hsv(rgb)
    vivid = (sat >= config.sat_min) & (val >= config.val_min)
    if stats is not None:
        stats.total_pixels = int(vivid.size)
        stats.color_pixels = int(vivid.sum())
    if not vivid.any():
        return []

    bright = (val >= config.text_val_min) & (sat <= config.text_sat_max)
    integral = np.pad(
        bright.astype(np.int32).cumsum(axis=0).cumsum(axis=1), ((1, 0), (1, 0))
    )

    span = 360.0 / HUE_BUCKETS
    buckets = np.clip((hue / span).astype(np.int32), 0, HUE_BUCKETS - 1)
    counts = np.bincount(buckets[vivid], minlength=HUE_BUCKETS)
    min_pixels = max(24, int(config.min_width * config.min_height * 0.3))
    order = [b for b in np.argsort(counts)[::-1] if counts[b] >= min_pixels]

    results: list[Detection] = []
    for bucket in order[:MAX_BUCKETS_SCANNED]:
        center = (bucket + 0.5) * span
        mask = vivid & (hue_distance(hue, center) <= span * 0.75)
        blobs = connected_components(mask)
        if stats is not None:
            stats.blobs += len(blobs)
        for x0, y0, x1, y1, area in blobs:
            width, height = x1 - x0, y1 - y0
            if not (config.min_width <= width <= config.max_width):
                continue
            if not (config.min_height <= height <= config.max_height):
                continue
            aspect = width / float(height)
            if not (config.min_aspect <= aspect <= config.max_aspect):
                continue
            fill = area / float(width * height)
            if fill < config.min_fill:
                continue
            bright_count = int(
                integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
            )
            text_ratio = bright_count / float(width * height)
            info = {
                "x": int(x0),
                "y": int(y0),
                "w": int(width),
                "h": int(height),
                "fill": float(fill),
                "text_ratio": float(text_ratio),
            }
            if not (config.min_text_ratio <= text_ratio <= config.max_text_ratio):
                if stats is not None:
                    stats.rejected.append(
                        (f"글자비율 {text_ratio:.3f} (색상 {center:.0f}°)", info)
                    )
                continue
            offset = label_offset(bright[y0:y1, x0:x1])
            if offset > config.max_label_offset:
                # 여기서 잘못 학습하면 설정 전체가 엉뚱한 UI 요소에 맞춰진다.
                # 스위치 손잡이처럼 밝은 부분이 쏠린 것은 후보에서 뺀다.
                if stats is not None:
                    stats.rejected.append(
                        (f"밝은 부분 쏠림 {offset:.2f} (스위치 모양)", info)
                    )
                continue

            # 실제 덩어리 픽셀의 색상 중심(원형 평균)
            inside = mask[y0:y1, x0:x1]
            hues = hue[y0:y1, x0:x1][inside]
            radians = np.deg2rad(hues)
            blob_hue = float(
                np.rad2deg(
                    np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())
                )
                % 360.0
            )
            score = button_likeness(
                width=width,
                height=height,
                fill=fill,
                text_ratio=text_ratio,
                hue=blob_hue,
                offset=offset,
            )
            results.append(
                Detection(
                    x=int(x0),
                    y=int(y0),
                    width=int(width),
                    height=int(height),
                    fill=round(float(fill), 3),
                    text_ratio=round(float(text_ratio), 3),
                    score=float(score),
                    hue=round(blob_hue, 1),
                )
            )

    results.sort(key=lambda d: d.score, reverse=True)
    return _suppress_overlaps(results)


def _suppress_overlaps(
    detections: list[Detection], threshold: float = 0.5
) -> list[Detection]:
    """색상 구간이 겹쳐 같은 버튼이 두 번 잡히는 것을 정리한다."""
    kept: list[Detection] = []
    for candidate in detections:
        if all(_iou(candidate, other) < threshold for other in kept):
            kept.append(candidate)
    return kept


def _iou(a: Detection, b: Detection) -> float:
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    union = a.width * a.height + b.width * b.height - overlap
    return overlap / float(union) if union else 0.0


def pick_target(
    detections: list[Detection], policy: str = "leftmost", row_tolerance: int = 12
) -> Detection | None:
    """클릭할 후보 하나를 고른다.

    match   : 견본과 가장 비슷한 것 (버튼 견본이 있을 때 기본)
    leftmost: 가장 버튼다운 후보와 같은 줄(row_tolerance 이내)에서 가장 왼쪽
              -> Kiro 대화상자의 'Allow'
    score   : 점수가 가장 높은 것
    """
    if not detections:
        return None
    if policy == "match" and any(d.match > -2.0 for d in detections):
        best = max(d.match for d in detections)
        # 일치도가 비슷하면(0.03 이내) 더 왼쪽에 있는 것을 고른다.
        close = [d for d in detections if best - d.match <= 0.03]
        return min(close, key=lambda d: (d.y // max(1, row_tolerance), d.x))
    if policy == "score":
        return detections[0]
    # 기준 줄은 '화면 맨 위'가 아니라 '가장 점수가 높은 후보가 있는 줄'이다.
    # 감시 영역이 넓으면 버튼보다 위쪽에 상관없는 요소(토글, 아이콘, 강조 표시)가
    # 들어오는데, 맨 위를 기준으로 잡으면 매번 그것을 클릭하게 된다.
    anchor = max(detections, key=lambda d: d.score)
    same_row = [d for d in detections if abs(d.y - anchor.y) <= row_tolerance]
    return min(same_row, key=lambda d: d.x)






@dataclass
class Calibration:
    """캘리브레이션 측정 결과."""

    config: DetectorConfig
    x: int  # 측정한 버튼의 bbox (crop 이미지 기준)
    y: int
    width: int
    height: int
    fill: float
    text_ratio: float
    hue: float
    sat: float
    val: float
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (
            f"버튼 크기 {self.width}x{self.height}, 채움율 {self.fill:.2f}, "
            f"글자비율 {self.text_ratio:.3f}, 색상 {self.hue:.0f}° "
            f"(채도 {self.sat:.2f}, 명도 {self.val:.2f})"
        )


def dominant_color(
    rgb: np.ndarray, sat_min: float = 0.18, val_min: float = 0.15
) -> tuple[float, float, float] | None:
    """영역에서 가장 많이 쓰인 '유채색'을 (색상, 채도, 명도) 로 돌려준다.

    버튼 중앙을 클릭하면 흰 글자 픽셀을 짚을 확률이 높다. 한 픽셀 색을 그대로
    쓰면 글자색을 버튼색으로 착각하므로, 주변에서 가장 흔한 색을 대표색으로 쓴다.
    """
    if rgb.size == 0:
        return None
    hue, sat, val = rgb_to_hsv(rgb)
    colored = (sat >= sat_min) & (val >= val_min)
    if int(colored.sum()) < 8:
        return None
    buckets = np.clip((hue[colored] / 10.0).astype(np.int32), 0, 35)
    dominant = int(np.bincount(buckets, minlength=36).argmax())
    selected = colored & (np.clip((hue / 10.0).astype(np.int32), 0, 35) == dominant)
    hues = hue[selected]
    radians = np.deg2rad(hues)
    center = float(
        np.rad2deg(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())) % 360.0
    )
    return center, float(np.median(sat[selected])), float(np.median(val[selected]))


def _similar_mask(
    hue: np.ndarray, sat: np.ndarray, val: np.ndarray, ref: tuple[float, float, float]
) -> np.ndarray:
    h0, s0, v0 = ref
    return (
        (hue_distance(hue, h0) <= 25.0)
        & (np.abs(sat - s0) <= 0.35)
        & (np.abs(val - v0) <= 0.35)
    )


def _nearest_true(
    mask: np.ndarray, px: int, py: int, radius: int = 24
) -> tuple[int, int] | None:
    """(px, py) 에서 가장 가까운 True 픽셀 (글자 위를 클릭했을 때 대비)."""
    height, width = mask.shape
    x0, x1 = max(0, px - radius), min(width, px + radius + 1)
    y0, y1 = max(0, py - radius), min(height, py + radius + 1)
    window = mask[y0:y1, x0:x1]
    ys, xs = np.nonzero(window)
    if ys.size == 0:
        return None
    dx = xs + x0 - px
    dy = ys + y0 - py
    index = int(np.argmin(dx * dx + dy * dy))
    return int(xs[index] + x0), int(ys[index] + y0)


def calibrate_at(
    rgb: np.ndarray,
    point: tuple[int, int],
    base: DetectorConfig | None = None,
) -> Calibration | None:
    """crop 이미지에서 (point) 주변의 버튼을 측정해 설정을 만든다.

    사용자가 실제 버튼을 클릭하면 주변 대표색으로 같은 덩어리를 찾아
    크기/채움율/글자비율/색 분포를 직접 재서 파라미터를 정한다.
    추측이 아니라 측정값이라 테마·배율·해상도가 달라도 맞는다.
    """
    if rgb.size == 0:
        return None
    px, py = int(point[0]), int(point[1])
    height, width = rgb.shape[:2]
    if not (0 <= px < width and 0 <= py < height):
        return None

    hue, sat, val = rgb_to_hsv(rgb)
    # 클릭 지점 주변(글자를 짚었을 수도 있으므로)에서 대표색을 뽑는다.
    radius = 24
    wx0, wx1 = max(0, px - radius), min(width, px + radius + 1)
    wy0, wy1 = max(0, py - radius), min(height, py + radius + 1)
    reference = dominant_color(rgb[wy0:wy1, wx0:wx1])
    if reference is None:
        reference = (float(hue[py, px]), float(sat[py, px]), float(val[py, px]))

    similar = _similar_mask(hue, sat, val, reference)
    seed = (px, py) if similar[py, px] else _nearest_true(similar, px, py, radius)
    if seed is None:
        return None

    blob = component_at(similar, seed[0], seed[1])
    if blob is None:
        return None

    x0, y0, x1, y1, area = blob
    bw, bh = x1 - x0, y1 - y0
    fill = area / float(bw * bh)

    inside = similar[y0:y1, x0:x1]
    hues = hue[y0:y1, x0:x1][inside]
    sats = sat[y0:y1, x0:x1][inside]
    vals = val[y0:y1, x0:x1][inside]

    # 호출자가 넘긴 설정을 직접 고치지 않도록 복사해서 쓴다.
    cfg = replace(base) if base is not None else DetectorConfig()
    # 색상: 원형 평균으로 중심을 잡고 퍼짐 정도로 허용 범위를 정한다.
    radians = np.deg2rad(hues)
    center = float(
        np.rad2deg(np.arctan2(np.sin(radians).mean(), np.cos(radians).mean())) % 360.0
    )
    spread = float(np.percentile(hue_distance(hues, center), 95))
    cfg.hue_center = round(center, 1)
    cfg.hue_tolerance = round(min(60.0, max(12.0, spread + 8.0)), 1)
    cfg.sat_min = round(max(0.05, float(np.percentile(sats, 5)) - 0.12), 3)
    cfg.val_min = round(max(0.05, float(np.percentile(vals, 5)) - 0.12), 3)

    bright = (val >= cfg.text_val_min) & (sat <= cfg.text_sat_max)
    text_ratio = float(bright[y0:y1, x0:x1].sum()) / float(bw * bh)
    offset = label_offset(bright[y0:y1, x0:x1])

    cfg.min_width = max(6, int(bw * 0.6))
    cfg.max_width = int(bw * 2.0) + 8
    cfg.min_height = max(4, int(bh * 0.6))
    cfg.max_height = int(bh * 2.0) + 8
    aspect = bw / float(bh)
    cfg.min_aspect = round(max(0.5, aspect * 0.55), 2)
    cfg.max_aspect = round(aspect * 2.0 + 1.0, 2)
    cfg.min_fill = round(max(0.30, fill * 0.85), 3)
    if text_ratio >= 0.005:
        cfg.require_text = True
        cfg.min_text_ratio = round(max(0.002, text_ratio * 0.4), 4)
        cfg.max_text_ratio = round(min(0.70, text_ratio * 2.5 + 0.05), 4)
        # 이 버튼의 라벨이 실제로 치우쳐 있다면(아이콘+글자 등) 그만큼 넓혀준다.
        # 그러지 않으면 방금 측정한 버튼이 '스위치 모양'으로 걸러진다.
        cfg.max_label_offset = round(
            max(DetectorConfig().max_label_offset, offset + 0.05), 3
        )
    else:
        # 글자를 못 찾았으면(아이콘 버튼 등) 글자 조건을 끈다.
        cfg.require_text = False

    warnings: list[str] = []
    if x0 <= 0 or y0 <= 0 or x1 >= width or y1 >= height:
        warnings.append(
            "측정한 덩어리가 캡처 경계에 닿았습니다. 버튼 중앙을 다시 클릭해 보세요."
        )
    if fill < 0.5:
        warnings.append(
            f"채움율이 낮습니다({fill:.2f}). 버튼이 아니라 테두리를 클릭했을 수 있습니다."
        )
    if bw * bh > (width * height) * 0.5:
        warnings.append("측정 영역이 너무 넓습니다. 배경이나 패널을 클릭했을 수 있습니다.")
    if not cfg.require_text:
        warnings.append("버튼 안에서 밝은 글자를 찾지 못해 글자 조건을 껐습니다.")

    return Calibration(
        config=cfg,
        x=int(x0),
        y=int(y0),
        width=int(bw),
        height=int(bh),
        fill=round(float(fill), 3),
        text_ratio=round(float(text_ratio), 4),
        hue=round(center, 1),
        sat=round(float(np.median(sats)), 3),
        val=round(float(np.median(vals)), 3),
        warnings=warnings,
    )


def calibrate_from_rect(
    crop: np.ndarray, base: DetectorConfig | None = None
) -> Calibration | None:
    """사용자가 드래그로 감싼 버튼 영역 자체를 측정해 설정을 만든다.

    사용자가 경계를 직접 알려준 경우라 flood fill 로 크기를 추정할 필요가 없다.
    그 영역의 대표색과 실제 크기를 그대로 기준으로 쓴다.
    """
    if crop.size == 0 or crop.shape[0] < 3 or crop.shape[1] < 3:
        return None
    reference = dominant_color(crop)
    if reference is None:
        return None

    hue, sat, val = rgb_to_hsv(crop)
    similar = _similar_mask(hue, sat, val, reference)
    height, width = crop.shape[:2]
    fill = float(similar.mean())

    cfg = replace(base) if base is not None else DetectorConfig()
    hues = hue[similar]
    sats = sat[similar]
    vals = val[similar]
    spread = float(np.percentile(hue_distance(hues, reference[0]), 95))
    cfg.hue_center = round(reference[0], 1)
    cfg.hue_tolerance = round(min(60.0, max(12.0, spread + 8.0)), 1)
    cfg.sat_min = round(max(0.05, float(np.percentile(sats, 5)) - 0.12), 3)
    cfg.val_min = round(max(0.05, float(np.percentile(vals, 5)) - 0.12), 3)

    bright = (val >= cfg.text_val_min) & (sat <= cfg.text_sat_max)
    text_ratio = float(bright.mean())
    offset = label_offset(bright)

    cfg.min_width = max(6, int(width * 0.6))
    cfg.max_width = int(width * 2.0) + 8
    cfg.min_height = max(4, int(height * 0.6))
    cfg.max_height = int(height * 2.0) + 8
    aspect = width / float(height)
    cfg.min_aspect = round(max(0.5, aspect * 0.55), 2)
    cfg.max_aspect = round(aspect * 2.0 + 1.0, 2)
    # 지정 영역에 여백이 조금 섞이는 것은 정상이므로 채움율 기준을 느슨하게 둔다.
    cfg.min_fill = round(max(0.30, fill * 0.75), 3)
    if text_ratio >= 0.005:
        cfg.require_text = True
        cfg.min_text_ratio = round(max(0.002, text_ratio * 0.4), 4)
        cfg.max_text_ratio = round(min(0.70, text_ratio * 2.5 + 0.05), 4)
        cfg.max_label_offset = round(
            max(DetectorConfig().max_label_offset, offset + 0.05), 3
        )
    else:
        cfg.require_text = False

    warnings: list[str] = []
    if fill < 0.55:
        warnings.append(
            f"지정한 영역에서 버튼 색이 {fill * 100:.0f}% 뿐입니다. "
            "버튼 테두리에 더 맞게 다시 지정하면 정확해집니다."
        )
    if not cfg.require_text:
        warnings.append("영역 안에서 밝은 글자를 찾지 못해 글자 조건을 껐습니다.")

    return Calibration(
        config=cfg,
        x=0,
        y=0,
        width=int(width),
        height=int(height),
        fill=round(fill, 3),
        text_ratio=round(text_ratio, 4),
        hue=round(reference[0], 1),
        sat=round(float(np.median(sats)), 3),
        val=round(float(np.median(vals)), 3),
        warnings=warnings,
    )
