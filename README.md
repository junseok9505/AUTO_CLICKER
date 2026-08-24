# Allow 자동 클릭기

화면의 지정한 영역만 감시하다가, 보라색 `Allow` 같은 버튼이 나타나면 자동으로 클릭한다.
Windows / macOS 공용이며, OS별로 다른 부분은 `allowclicker/platforms/` 안에만 있다.

## 요구 사항

- Python 3.10 이상
- tkinter (Windows 공식 설치본에는 포함. macOS는 아래 참고)
- 의존성 2개: `mss`, `numpy`

## 설치 및 실행

명령은 두 OS가 같다. `python` 실행 파일 이름만 다르다 (Windows `python`, macOS `python3`).

```bash
# 1. 의존성 설치
python -m pip install -r requirements.txt      # macOS: python3 -m pip ...

# 2. 실행
python run.py                                  # macOS: python3 run.py
```

가상환경을 쓰는 경우 활성화 명령만 다르다.

```bash
python -m venv .venv                           # macOS: python3 -m venv .venv
.venv\Scripts\Activate.ps1                     # macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
```

`python -m allowclicker` 로 실행해도 동일하다.

## macOS 추가 준비

macOS 기본 `python3` 에는 tkinter 가 없을 수 있다. 없으면 아래 중 하나로 설치한다.

```bash
brew install python-tk
# 또는 python.org 공식 설치본 사용
```

권한 두 개를 허용해야 동작한다. 둘 다 없으면 **오류 없이 조용히 실패**하므로,
프로그램이 시작할 때 권한 상태를 확인해서 로그에 경고를 남긴다.

- 시스템 설정 → 개인정보 보호 및 보안 → **화면 기록**: 없으면 배경화면만 캡처되어 버튼을 못 찾는다
- 시스템 설정 → 개인정보 보호 및 보안 → **손쉬운 사용**: 없으면 클릭이 무시된다

터미널에서 실행하면 권한은 터미널에 부여된다. macOS 15 이상에서는 화면 캡처 권한
프롬프트가 반복될 수 있다. 그럴 때는 검사 간격을 늘리거나 앱 번들로 만들어 실행한다.

> macOS 구현은 CoreGraphics를 직접 호출하도록 작성되어 있으나 실기 검증은 아직 하지
> 않았다. 처음 실행할 때는 `테스트 모드`(클릭 안 함)로 인식이 되는지 먼저 확인하는 것이 좋다.

## 사용 순서

1. **모니터** 선택 (해상도나 모니터 구성이 바뀌면 `새로고침`)
2. **버튼 영역 지정** — 눌러야 하는 버튼을 드래그로 감싼다. 색·크기·모양을 그 버튼에서
   측정해 기준으로 삼고, 감시 영역도 버튼 주변으로 잡아준다.
   (버튼만 잘라낸 PNG/GIF 가 있으면 `이미지로 지정` 도 가능)
3. **한 번 검사** 로 미리보기의 초록 사각형이 실제 버튼과 맞는지 확인
4. **시작**

버튼 영역을 지정하지 않아도 된다. 그 경우 시작할 때 영역 안에서 버튼처럼 보이는 것을
스스로 찾아 기준을 학습한다(자동 캘리브레이션). 시작 시점에 버튼이 없으면 나타날 때까지
기다렸다가 그때 학습한다.

## 동작과 안전장치

- 감시 중 **F8** 을 누르면 즉시 정지 (macOS 노트북은 `fn` + F8)
- `테스트 모드`: 감지만 하고 클릭하지 않음
- 같은 위치에서 연속 감지되어야 클릭 (렌더링 중간 오클릭 방지)
- 클릭 전에 커서가 실제로 도착한 좌표를 확인하고, 어긋나면 그 차이를 학습해 보정
- 눌렀는데 버튼이 남아 있으면 사라질 때까지 다시 클릭 (한 버튼당 제한 시간 있음)
- 해상도나 모니터 배치가 바뀌면 좌표가 무의미해지므로 자동으로 정지
- 클릭 후 마우스를 원래 위치로 복귀

> 승인 버튼을 자동으로 누르는 도구다. 확인 없이 실행되면 곤란한 작업이 있을 수 있으니
> 감시 영역은 필요한 버튼 주변으로 좁게 잡는 것이 안전하다.

## 설정 저장

조작할 때마다 자동 저장되고 다음 실행에서 복원된다. 저장 위치만 OS별로 다르다.

- Windows: `%APPDATA%\AllowClicker\config.json`
- macOS: `~/Library/Application Support/AllowClicker/config.json`

감시 영역, 버튼 영역, 견본 이미지, 인식 기준, 학습된 클릭 보정값, 각종 옵션이 저장된다.

## 구조

```
run.py                     실행 진입점
allowclicker/
  detector.py              버튼 탐지 / 캘리브레이션 / 견본 비교 (numpy만 사용)
  capture.py               화면 캡처 (mss)
  worker.py                감시 루프 (탐지 → 좌표 검증 → 클릭 → 재시도)
  config.py  geometry.py
  platforms/               OS별 구현 (windows.py / macos.py / base.py)
  ui/                      tkinter UI (app.py / overlay.py)
```

## 문제가 생기면

`한 번 검사` 를 누르면 왜 못 찾았는지 로그에 남는다. 지정한 색이 영역 안에 몇 퍼센트
있는지, 후보가 어떤 조건에서 탈락했는지(크기·비율·채움율·글자비율·견본 일치도), 색을
무시한 자동 탐지로는 무엇이 보이는지까지 표시한다. 기준이 꼬였으면 `기본값` 으로 되돌린 뒤
버튼 영역을 다시 지정하면 된다.
