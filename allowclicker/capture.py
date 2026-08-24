"""화면 캡처 (mss 기반, Windows/macOS 공용).

mss 는 두 OS 모두에서 동작한다. Retina(macOS) 처럼 논리 좌표와 물리 픽셀이
다른 환경에서는 요청한 폭과 실제 반환된 이미지 폭의 비율로 scale 을 계산해서
호출자가 좌표를 되돌릴 수 있게 한다.
"""

from __future__ import annotations

import threading

import numpy as np

from .geometry import Region

try:
    import mss
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "mss 패키지가 필요합니다.  pip install -r requirements.txt 를 먼저 실행하세요."
    ) from exc


class ScreenCapture:
    """스레드별 mss 인스턴스를 유지하는 캡처 도구.

    mss 는 스레드 안전하지 않으므로 thread-local 로 인스턴스를 만든다.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            # mss 10.2 부터 소문자 mss.mss() 는 deprecated (11.0 에서 제거 예정).
            # 새 API 를 우선 쓰고, 구버전에서는 예전 팩토리로 폴백한다.
            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
            self._local.sct = sct
        return sct

    def grab(self, region: Region) -> tuple[np.ndarray, float]:
        """영역을 캡처해서 (RGB uint8 배열, scale) 반환."""
        shot = self._sct().grab(
            {
                "left": int(region.x),
                "top": int(region.y),
                "width": int(region.width),
                "height": int(region.height),
            }
        )
        buf = np.frombuffer(shot.bgra, dtype=np.uint8)
        img = buf.reshape(shot.height, shot.width, 4)[:, :, :3]
        rgb = img[:, :, ::-1].copy()  # BGRA -> RGB
        scale = shot.width / float(region.width) if region.width else 1.0
        return rgb, scale

    def monitors(self, *, refresh: bool = False) -> list[dict]:
        """모니터 목록. [0] 은 전체(가상 데스크톱), [1:] 는 개별 모니터.

        mss 가 캡처에 실제로 사용하는 좌표계를 그대로 쓰므로,
        여기서 얻은 좌표는 grab() 좌표와 항상 일치한다.
        mss 는 목록을 캐시하므로 해상도/배치가 바뀌면 refresh=True 로 다시 연다.
        """
        if refresh:
            self.close()
        return [dict(m) for m in self._sct().monitors]

    def sample_color(self, x: int, y: int, size: int = 5) -> tuple[int, int, int]:
        """(x, y) 주변 픽셀 평균 색을 RGB 로 반환."""
        half = max(1, size // 2)
        region = Region(int(x) - half, int(y) - half, half * 2 + 1, half * 2 + 1)
        rgb, _ = self.grab(region)
        mean = rgb.reshape(-1, 3).mean(axis=0)
        return int(mean[0]), int(mean[1]), int(mean[2])

    def close(self) -> None:
        sct = getattr(self._local, "sct", None)
        if sct is not None:
            try:
                sct.close()
            except Exception:  # pragma: no cover
                pass
            self._local.sct = None
