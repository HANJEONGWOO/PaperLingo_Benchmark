# PaperLingo 채점 워크스페이스

논문 초록에서 생성한 학습 표현과 사지선다 문항을 평가하는 웹앱입니다. 참조 라벨과의 표현 일치도와 보기의 네 가지 기본 조건을 코드로 검사합니다. 기본 모드에서는 LLM 호출이나 사람 검수 대기 없이 통과·실패와 최종 점수를 표시합니다.

공유 주소: **http://hjw.iptime.org:4173/**

현재 구성은 모든 방문자가 로그인 없이 결과 업로드, 자동 코드 검사, 기록 조회·비교 및 다운로드를 할 수 있는 공개 워크스페이스입니다. LLM 추론 서버는 `127.0.0.1:11434`에서만 열고, 외부에는 웹앱의 4173 포트만 제공합니다. 추가 유료 API를 호출하지 않습니다.

## 사용 방법

1. **데이터** 화면에서 학습·평가 자료를 내려받습니다. 형식을 확인하려면 기존 예제를 불러올 수 있습니다.
2. 생성 결과를 업로드하고 **형식 예제 / 학습 70% / 평가 30%** 중 해당 범위를 선택합니다.
3. Precision·Recall·F1과 논문별 참조 표현의 일치/누락을 확인합니다.
4. **보기 형식 / 중복 여부 / 지정 정답 / 원문 일치 정답의 유일성**을 즉시 검사합니다. 문항별 통과·실패와 사유를 확인합니다.
5. **Q · 일치 문항 통과율**과 최종 점수를 확인합니다. 코드 검사에 LLM·사람 검수·임시 만점 버튼은 사용하지 않습니다.
6. **평가 기록**에서 같은 데이터·논문 범위·검사 기준의 두 결과를 비교하거나 JSON/CSV를 내려받습니다.

형식 예제는 참조 라벨을 참고해 직접 작성한 자료이며 생성 모델의 실측 성능이 아닙니다. 논문 3편의 참조 표현을 빠짐없이 담았습니다(4개·5개·0개, 총 9개). 코드 검사 모드에서는 표현 일치도, 네 가지 검사와 최종 점수가 모두 100%입니다. 이는 의미적 정답성이나 교육적 품질을 보증하지 않습니다.

기존 LLM·사람 검수 및 임시 만점 기록은 당시의 점수와 기준을 유지합니다. 새로운 코드 검사 점수는 같은 입력을 다시 제출해 확인하세요. 이전 기록의 사람 검수와 임시 처리 기능은 호환 목적으로 유지하되 새 코드 검사 결과를 덮어쓰지는 못합니다.

상단의 **한국어 / English** 버튼으로 메뉴, 입력 안내, 결과와 검사 사유의 언어를 바꿀 수 있습니다. 선택은 해당 주소의 브라우저 `localStorage`에 저장해 새로고침하거나 다시 방문해도 유지합니다. 첫 방문에는 브라우저 언어를 따르며, 한국어 이외의 언어는 영어로 표시합니다. 전환 시 페이지를 다시 불러오지 않아 작성 중인 입력, 필터, 펼친 문항을 유지합니다. 같은 주소의 다른 탭에도 선택이 반영되며, 저장소 사용이 차단된 환경에서는 현재 탭의 전환만 지원합니다. 논문 원문, 제출한 보기·이름, 사람이 작성한 검수 의견과 다운로드 데이터는 원문을 유지합니다. 영어 필드 설명은 `/api/schema?lang=en`에서 확인할 수 있습니다.

## 설치 및 실행

Python 3.12 이상에서:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m paperlingo
```

설정은 `.env` 또는 서버 환경변수에서 읽습니다. 기존 환경변수가 우선합니다. `.env.example`에는 유료 API 키가 없으며, 공개 모드를 명시적으로 활성화합니다. `PAPERLINGO_REVIEW_MODE=code`가 기본값이며 모델 서버가 중지되어도 전체 코드 검사와 채점이 가능합니다. 기존 LLM 검수를 운영자가 다시 활성화하려면 `PAPERLINGO_REVIEW_MODE=llm`으로 변경하고 웹 서비스를 재시작합니다. 이 설정은 새 평가와 기존 미완료 LLM 작업에 적용되며 완료된 코드 검사 기록은 변경하지 않습니다.

현재 PC에는 사용자 systemd 서비스 `paperlingo-web`를 등록하여 WSL이 실행 중일 때 자동 시작하도록 구성했습니다.

```bash
systemctl --user status paperlingo-web
systemctl --user restart paperlingo-web
journalctl --user -u paperlingo-web -n 80 --no-pager
```

설정이나 Python 코드를 수정하면 웹 서비스를 다시 시작합니다. HTML/CSS/JS 변경은 브라우저를 새로고침하면 반영됩니다. 현재 작업 큐는 단일 프로세스를 기준으로 하므로 Uvicorn worker는 **1개**로 실행합니다.

로컬 모델 실행 방법과 GPU 환경은 [로컬 추론 서버 안내](docs/README-local-runtime.md)를 확인하세요. `JUDGE_BASE_URL`, `JUDGE_MODEL`을 변경하면 다른 OpenAI 호환 로컬 서버도 연결할 수 있습니다. `JUDGE_API_KEY`는 인증이 필요한 서버를 사용할 때만 설정하며 브라우저에 전송되지 않습니다.

## DDNS / WSL 연결

현재 연결 경로:

```text
hjw.iptime.org:4173
  → 공유기: 외부 TCP 4173 → Windows PC 192.168.0.28:4173
  → Windows portproxy: TCP 4173 → WSL 172.17.121.216:4173
  → PaperLingo 웹앱
  → 로컬 GPU 추론 서버 127.0.0.1:11434
```

기존 Windows portproxy에 `0.0.0.0:4173 → 172.17.121.216:4173` 설정이 있음을 확인했습니다. 공유기/Windows의 기존 다른 규칙은 변경하지 않았습니다. WSL이 중지되면 웹앱과 모델도 중지됩니다. Windows 재부팅 후에는 WSL을 실행해야 하며, WSL IP가 바뀌면 해당 포트 연결도 갱신해야 합니다.

필요할 때만 관리자 PowerShell에서 [포트 연결 스크립트](deploy/windows-port-forward.ps1)를 실행합니다. 외부·내부 포트는 모두 4173입니다.

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows-port-forward.ps1 -Port 4173
```

접속 확인:

```bash
curl http://127.0.0.1:4173/api/health
curl http://hjw.iptime.org:4173/api/health
```

공용 주소는 HTTP입니다. 팀 비밀번호 모드로 바꿀 경우에는 HTTPS 프록시를 함께 설정하고 `PAPERLINGO_SECURE_COOKIE=true`로 변경하세요. 공개 모드에는 로그인 비밀번호 입력이 필요하지 않습니다.

## 생성 모델 결과 형식

입력은 논문당 한 줄의 JSONL 또는 JSON 객체 배열입니다. 한 논문에서 0~5개 문항을 생성합니다. 논문별 필드는 `paper_id`, `questions`이며 **각 문항은 아래 세 속성만 작성합니다.**

| 필드 | 의미 |
|---|---|
| `expression_text` | 선정한 전체 표현. 원문 `abstract`를 그대로 사용 |
| `options` | 서로 다른 보기 문자열 4개의 배열 |
| `answer_id` | 정답 보기의 **배열 인덱스 0~3**. 정수 `0`은 첫 번째 보기 |

```json
{
  "paper_id": "0911.3369",
  "questions": [
    {
      "expression_text": "identical to",
      "options": ["to", "with", "of", "for"],
      "answer_id": 0
    }
  ]
}
```

서버가 원본 `abstract`에서 표현을 찾고, `options[answer_id]`에 해당하는 부분을 빈칸으로 바꿉니다. 위 예제는 `identical [BLANK]` 문제가 됩니다. 정답 문자열은 전체 표현 안에 온전한 단어 또는 연속된 구절로 존재해야 합니다. 문맥은 표현 전체를 포함하는 원문 문장 구간으로 자동 구성합니다.

같은 표현이 초록에 반복되면 **첫 번째 등장 위치**를 사용하고, 정답 문자열이 표현 안에서 반복되어도 **첫 번째 위치**를 빈칸으로 만듭니다. 띄어쓰기·대소문자를 포함해 원문과 정확히 일치해야 하며 단어 일부만 잘라내지는 않습니다. 표현이 문장 경계를 가로지르면 표현 전체를 포함하는 구간을 사용합니다. 생성된 문맥과 빈칸은 결과 화면에서 확인할 수 있습니다.

`question_id`와 세 위치 필드(`expression_span`, `context_span`, `blank_span`)는 서버에서 계산하므로 입력할 필요가 없습니다. `expression_type`과 `learning_point`도 입력에서 제거했습니다. 이전 형식의 제출과 저장된 기록은 계속 읽을 수 있습니다. 결과·검수 API는 기존 기록과 호환되도록 서버 문항 ID, 위치, A/B/C/D 보기 ID를 포함합니다.

한 문항 안에 새 문자열 보기·정수 인덱스와 이전 형식의 ID·위치 필드를 섞으면 제출 오류가 됩니다. 새 형식은 위 세 속성만 사용하세요.

실행 가능한 예제는 `examples/predictions.jsonl` 또는 웹의 예제 다운로드에서 확인하세요. 필드 설명은 `/api/schema`에서도 제공합니다. 잘못된 JSON, 중복 논문 ID, 알 수 없는 필드, 범위 밖의 논문, 5개 초과는 제출 오류입니다. 정답 인덱스 오류·보기 중복·정답 문자열의 원문 불일치 등은 해당 문항을 삭제하지 않고 품질 0점으로 남깁니다. `answer_id`에 `"0"`, `true`, `4`를 넣으면 유효한 정답 인덱스로 인정하지 않습니다.

## 데이터와 평가 범위

원본 파일은 수정하지 않습니다.

- 초록: `data/abstract_data.jsonl` — 1,490건
- 참조 라벨: `data/abstract_labels_v2.jsonl` — 6,188개 표현
- 선정 기준: `data/labeling_prompt_v2.md`
- 149개 분야에서 분야별 10편, 참조 표현이 없는 초록 12편
- 분야별 seed 42 고정 분할: **학습 1,043편(70%) / 평가 447편(30%)**, 분야마다 7편/3편
- 형식 예제 3편은 학습 세트에 포함되며 평가 세트와 겹치지 않습니다.
- `/api/splits?download=1`에서 데이터 해시, 분할 버전 `train-evaluation-v1`, 실제 논문 ID 목록 다운로드

웹의 **데이터** 화면 상단에서 다음 파일을 내려받을 수 있습니다. 새 평가 입력 화면에서도 데이터 화면으로 이동할 수 있습니다. 모든 파일은 논문 한 편을 한 줄에 담는 JSONL이며, 같은 범위의 파일은 논문 ID와 순서가 일치합니다.

| 범위 | 논문 초록 | 선정 표현 정답 | 사지선다 예시답안 |
| --- | --- | --- | --- |
| 학습 70% · 1,043편 | `/api/datasets/train/papers` | `/api/datasets/train/labels` | `/api/datasets/train/answers` |
| 평가 30% · 447편 | `/api/datasets/evaluation/papers` | 비공개 · 버튼 비활성화 | 비공개 · 버튼 비활성화 |

초록 파일에는 `paper_id`, `paper_name`, `domain`, `categories`, `abstract`가 들어갑니다. 선정 표현 파일에는 `paper_id`, `expressions`와 원본의 표현·유형·선정 근거가 들어갑니다. 선정 표현과 사지선다 예시답안은 학습 세트에만 제공합니다. 평가용 `/api/datasets/evaluation/labels`와 `/api/datasets/evaluation/answers`는 직접 요청해도 404를 반환합니다.

학습 사지선다 파일은 학습 세트의 참조 표현 4,324개를 모두 담은 `compact-v1` 제출 형식입니다. 원본 라벨에는 사지선다 보기가 없어, 표현의 전치사·접속사·동사 활용형 등에 맞는 규칙으로 추가 작성했습니다. 정답 인덱스는 원문 표현에 해당하는 보기를 가리키며 위치는 문항별로 분산합니다. **보기의 유일 정답 여부와 교육적 품질은 미검수 상태입니다.** 검증된 품질 정답이나 모델 성능으로 취급하지 않으며, 모델 학습에 사용할 때는 필요한 보기 검수를 거쳐 사용하세요. 다운로드는 LLM 추론이나 평가 기록을 만들지 않습니다.

평가 논문 탐색과 상세 API에서도 선정 표현을 공개하지 않습니다. 평가 결과의 화면·JSON은 참조 표현과 누락 표현 목록 대신 비공개 안내 및 개수를 제공하며, 제출한 문항의 일치 여부와 점수는 확인할 수 있습니다. 과거 전체 범위 기록에도 현재 평가 세트에 속한 논문에는 같은 규칙을 적용합니다. 저장된 원본 라벨·기록·점수는 변경하지 않고 응답에서만 정답 목록을 숨깁니다.

평가할 범위를 제출 전에 선택합니다. 해당 범위에 포함된 미제출 논문도 Recall 분모에 남습니다. 신규 API는 `demo`, `train`, `evaluation`만 허용하며, 폐지한 범위를 새 범위로 자동 변환하지 않습니다. 기존 개발·검증·최종 평가·전체·직접 지정 기록은 원래 논문 목록과 점수를 유지합니다. 서로 다른 범위의 결과를 비교하면 화면에 안내합니다. 선정 표현의 힌트 없이 생성기의 표현 선정 성능을 측정하려면 초록 파일만 입력으로 사용하세요.

```bash
.venv/bin/python scripts/export_inputs.py --scope demo --output runtime/demo-abstracts.jsonl
```

선택적 예제 생성기:

```bash
GENERATOR_BASE_URL=http://127.0.0.1:11434/v1 \
GENERATOR_MODEL=Qwen/Qwen3-8B \
GENERATOR_TIMEOUT=300 \
.venv/bin/python scripts/example_generator.py \
  --input runtime/demo-abstracts.jsonl --output runtime/predictions.jsonl --limit 3
```

생성기는 참조 라벨을 읽지 않습니다. 생성과 검수에 같은 모델을 사용할 수는 있지만, 그 점수만으로 평가기의 신뢰성이 확인되는 것은 아닙니다. 필요할 때 사람의 표본 검수로 확인할 수 있습니다.

## 채점 규칙

`T`: 일대일로 정확히 일치한 표현 수, `N`: 제출 표현 수, `G`: 고정 평가 범위의 참조 표현 수입니다.

```text
Precision = T / N
Recall    = T / G
F1        = 2T / (N + G)
q         = 입력 구조가 유효하고 네 가지 검사 모두 통과하면 1, 아니면 0
Qmatched  = 참조 라벨과 일치한 문항 중 검사 통과 비율
Final     = 100 × Recall × Qmatched
```

예: 참조 표현 10개 중 6개를 찾고 그중 5개 문항이 통과했다면 `100 × (6/10) × (5/6) = 50점`입니다. Precision과 F1은 보조 지표이며 최종 점수에는 곱하지 않습니다. 논문당 0~5개를 제출할 수 있고 Recall의 분모는 평가 범위의 실제 참조 표현 수입니다. 미제출 논문의 참조 표현도 포함합니다.

코드 검사 기준은 다음과 같습니다.

| 검사 | 통과 조건 |
| --- | --- |
| 보기 형식 | 비어 있지 않은 문자열 4개, 각 1~1000자 |
| 중복 여부 | 유니코드 NFKC·대소문자·공백 정규화 후 모든 보기 문자열이 다름 |
| 지정 정답 | `answer_id`가 0~3의 정수이며 해당 보기로 원문 빈칸을 정확히 복원함 |
| 유일한 정답 · 원문 일치 | 원문 빈칸과 정규화 후 일치하는 보기가 정확히 하나 |

**문맥 충분성, 의미적으로 허용되는 다른 정답, 오답의 그럴듯함은 검사하지 않습니다.** 간단 입력 형식은 지정 정답으로 빈칸을 구성하므로, 지정 정답 검사는 원문과의 일관성 확인입니다. 보기의 독립적인 의미적 정답 판정이 아닙니다. 유사한 뜻의 보기가 있어도 문자열이 다르면 이 검사를 통과할 수 있습니다. `unique_answer_rate_pct`는 미평가를 뜻하는 `null`, `source_unique_answer_rate_pct`는 원문 일치 유일률입니다. 두 지표를 혼용하지 않습니다.

원문에 없는 표현, 잘못된 위치, 중복 표현 등 기존 입력 무결성 검증은 유지하며 위반 문항은 실패로 남깁니다. 각 검사 결과와 실패 사유는 화면과 JSON에, 검사별 통과 여부는 CSV에도 포함합니다. 결과는 `pass`/`fail`, `q`는 1/0이며 실패 문항을 분모에서 제거하지 않습니다.

표현은 같은 논문 안에서 전체 원문 문자열로 정확히 매칭합니다. 중복 출력은 추가 일치로 인정하지 않습니다. 라벨 밖 표현은 최종 점수를 높이거나 낮추지 않으며 Precision·F1과 전체 문항 통과율에는 반영됩니다. 모든 제출 문항을 코드로 검사합니다.

- 참조 표현이 있으나 제출 문항이 없는 경우 최종 점수는 0입니다.
- 참조 표현이 없는 범위의 Recall과 최종 점수는 해당 없음(`null`)입니다.
- 참조가 빈 논문은 명시적인 빈 응답일 때만 올바른 미출력으로 인정합니다.

현재 점수는 참조 라벨과의 일치 및 기본 입력 조건의 충족 여부를 측정합니다. 실제 학습 효과를 측정하지 않습니다. 라벨 밖의 좋은 표현도 있을 수 있으므로 라벨이나 판정 기준을 변경하면 버전을 고정하고 모든 모델을 같은 기준으로 다시 비교하세요.

신규 평가의 지표 버전은 `exact-rq-v3`, 코드 검사 정책은 `code-basic-v1`입니다. 이전 `auto-conservative-v1` LLM·사람 검수 점수 및 `exact-prq-v1`·`exact-prq-v2`의 산식(`100 × Precision × Recall × Qmatched`)은 유지합니다. 서로 다른 검사 정책의 점수는 직접 비교하지 않습니다.

선택적인 이전 `llm` 모드에서는 정답 키를 숨긴 문맥과 보기로 허용 정답을 판정하고, 4개 품질 항목의 합계를 8로 나눈 값을 q로 사용합니다. 단일 정답 등 필수 조건 실패나 불확실 판정은 0, API 오류는 미완료입니다. 사람 검수 및 임시 만점 처리는 이 이전 방식의 기록에만 적용됩니다. 자세한 운영법은 [로컬 추론 서버 안내](docs/README-local-runtime.md)를 참고하세요.

## 저장·백업·검증

평가 기록과 검수 이력은 `runtime/paperlingo.sqlite3`에 저장합니다. 테스트는 임시 DB와 모의 LLM으로 실행하며 유료 API를 호출하지 않습니다.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

언어 전환의 브라우저 검증은 Playwright와 Chromium이 있을 때 실행합니다. 입력 보존, 언어 저장·복원, 다른 탭과의 동기화, 검사 사유 번역 및 모바일 화면을 임시 서버·DB에서 확인합니다. 브라우저 의존성이 없으면 해당 검사는 건너뜁니다.

```bash
.venv/bin/python -m pip install -r requirements-ui.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest -q tests/test_ui_language.py
```

백업 시에는 서비스를 잠시 중지한 뒤 runtime 디렉터리 전체를 복사하거나 SQLite의 backup API를 사용합니다. 데이터셋·지표·검사 정책 버전과 평가 범위는 결과 JSON에 남습니다. LLM 모드에서는 모델명, 프롬프트 해시, 사용 토큰, 지연 시간과 검수 이력도 저장합니다. 코드 모드에서는 이전 LLM 작업을 자동 재개하지 않습니다. `llm` 모드에서 모델이 설정되어 있으면 서버 재시작 후 대기·중단된 작업을 자동 재개하며 이미 완료된 판정을 보존합니다. 이전 버전에서 검수를 기다리던 기록도 미완료 문항만 자동 처리합니다. 완료된 임시 만점 기록은 자동 재검수하지 않으며 사용자가 모델 검수를 선택할 때 대체합니다.

주요 파일:

- `paperlingo/app.py`: HTTP API, 작업 실행, 공개/팀 접근
- `paperlingo/dataset.py`, `scoring.py`: 데이터 감사, 분할, 정확 매칭과 집계
- `paperlingo/code_review.py`: 네 가지 코드 검사와 통과·실패 판정
- `paperlingo/judge.py`, `prompts/judge_v1.txt`: 블라인드 LLM 검수와 공통 rubric
- `paperlingo/static/`: 웹 화면
- `paperlingo/static/i18n.js`, `translations.js`: 언어 선택 저장, 화면 문구 연결 및 영어 번역
- `scripts/example_generator.py`: 초록만 입력하는 예제 생성기
- `scripts/local_judge_server.py`: 로컬 GPU 추론 서버
- `tests/`: 채점, 평가기, API, 예외 처리 테스트

구현 시 참고: [FastAPI 서버 실행](https://fastapi.tiangolo.com/deployment/manually/), [Pydantic 검증 모델](https://docs.pydantic.dev/latest/concepts/models/), [OpenAI 공식 JSON 출력 문서](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
