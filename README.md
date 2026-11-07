# PaperLingo 채점 워크스페이스

논문 초록에서 생성한 학습 표현과 사지선다 문항을 평가하는 웹앱입니다. 참조 라벨과의 표현 일치도는 코드로 계산하고, 오답 보기의 품질은 로컬 LLM 또는 사람이 검수합니다.

공유 주소: **http://hjw.iptime.org:4173/**

현재 구성은 모든 방문자가 로그인 없이 결과 업로드, 자동 채점, LLM 검수, 수동 검수, 기록 조회·비교 및 다운로드를 할 수 있는 공개 워크스페이스입니다. LLM 추론 서버는 `127.0.0.1:11434`에서만 열고, 외부에는 웹앱의 4173 포트만 제공합니다. 추가 유료 API를 호출하지 않습니다.

## 사용 방법

1. 웹에서 **예제 불러오기 → 표현 채점 시작**을 누릅니다.
2. Precision·Recall·F1과 논문별 참조 표현의 일치/누락을 확인합니다.
3. **로컬 LLM 보기 검수**를 누르거나 각 문항의 **사람 검수**를 진행합니다.
4. 모든 제출 문항의 검수가 끝나면 보기 품질과 최종 점수가 확정됩니다.
5. **평가 기록**에서 같은 데이터·논문 범위의 두 결과를 비교하거나 JSON/CSV를 내려받습니다.

예제는 입력 형식과 구조 오류를 보여주기 위해 직접 작성한 자료입니다. 생성 모델의 실측 성능을 뜻하지 않습니다. 예제에는 정상 문항과 중복 보기 문항, 참조 표현이 없는 논문이 포함됩니다. 중복 보기 문항은 구조 오류로 품질 0점이며 수동 검수로 덮어쓸 수 없습니다.

LLM 검수는 한 번에 최대 20개씩 실행하며 실행 중인 작업이 끝난 뒤 다음 작업을 시작할 수 있습니다. 남은 문항이 있으면 다시 누릅니다. 사람 검수와 LLM 검수는 같은 점수 계산 규칙을 사용하고, 판정의 출처·검수자·근거·변경 이력이 저장됩니다. 공개 모드의 검수자 이름은 본인이 입력한 식별자입니다.

사람 검수의 보기 체크박스는 **문맥상 정답으로 허용할 보기**를 선택하는 항목입니다. 허용 정답이 정확히 하나이고 생성기의 지정 정답과 같으며 필수 조건을 모두 충족해야 품질 항목 점수가 반영됩니다. 품질 항목을 모두 2점으로 주어도, 다른 보기를 유일한 정답으로 선택하면 문항 품질은 0점입니다. 정답이 없거나 복수인 경우도 해당 판정을 그대로 저장할 수 있습니다. 검수 창에서 저장 예상 품질과 조건별 사유를 확인하고, 결과의 문항별 0점 사유를 확인하세요.

## 설치 및 실행

Python 3.12 이상에서:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m paperlingo
```

설정은 `.env` 또는 서버 환경변수에서 읽습니다. 기존 환경변수가 우선합니다. `.env.example`에는 유료 API 키가 없으며, 공개 모드를 명시적으로 활성화합니다. 서버가 중지된 상태에서도 수동 검수로 전체 평가를 완료할 수 있습니다.

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
- 분야별 seed 42 고정 분할: 개발 1,043 / 검증 149 / 최종 평가 298편
- `/api/splits?download=1`에서 데이터 해시와 분할 manifest 다운로드

평가할 범위를 제출 전에 선택합니다. 해당 범위에 포함된 미제출 논문도 Recall 분모에 남습니다. 임의로 제출된 논문만 골라 분모를 줄이지 않습니다. 사용자 지정 범위는 명시적인 논문 ID 목록이 필요하며, 서로 다른 범위의 결과를 비교하면 화면에 안내합니다. 참조 데이터 화면은 팀 점검용으로 정답을 공개하므로, 생성기에는 아래 명령으로 내보낸 초록만 전달하세요.

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

생성기는 참조 라벨을 읽지 않습니다. 생성과 검수에 같은 모델을 사용할 수는 있지만, 그 점수만으로 평가기의 신뢰성이 확인되는 것은 아니므로 사람의 표본 검수를 함께 진행하세요.

## 채점 규칙

`T`: 일대일로 정확히 일치한 표현 수, `N`: 제출 표현 수, `G`: 고정 평가 범위의 참조 표현 수입니다.

```text
Precision = T / N
Recall    = T / G
F1        = 2T / (N + G)
q         = 필수 조건을 통과한 문항의 rubric 합계 / 8
Qmatched  = 참조 라벨과 일치한 문항들의 평균 q
Final     = 100 × Precision × Recall × Qmatched
```

표현은 같은 논문 안에서 원문 문자열로 정확히 매칭합니다. 유형과 canonical_form은 주 점수에 사용하지 않습니다. 표현의 일부가 빈칸이어도 전체 표현을 비교합니다. 중복 출력은 추가 일치로 인정하지 않고 FP로 남깁니다.

LLM의 보기 품질 평가는 정답 키·참조 표현·원문 정답 문장·생성기의 해설을 숨긴 상태에서 학습자에게 보이는 문맥과 4개 보기만으로 판정합니다. 선택지 순서는 문항별 결정적인 seed로 섞고, 각 보기의 acceptable/incorrect/uncertain 판정 및 근거를 기록합니다.

- 필수 조건: 정답이 정확히 하나이고 생성기 정답과 일치, 충분한 문맥, 전문 지식 불필요, 직접 정답 노출 없음
- 품질 항목: 그럴듯함 / 학습 목표 관련성 / 오답 간 구별성 / 불필요한 단서 억제, 각 0~2점
- 구조 오류·필수 조건 실패: `q=0`
- 모델의 불확실 판정: 재검토 필요, 점수 미확정
- 추론 서버/JSON 응답 오류: 재시도 필요, 점수 미확정
- 모든 제출 문항의 품질이 확정되기 전: 최종 점수 `null`
- G>0이고 N=0인 명시적 빈 제출: 최종 점수 0
- G=0인 범위: Recall/최종 점수 해당 없음, 미출력 적절성과 FP 별도 확인
- 참조가 빈 논문은 명시적인 빈 응답일 때만 올바른 미출력으로 인정

이 지표는 기존 LLM 라벨과의 일치 및 문항 적합성을 측정합니다. 실제 학습 효과를 직접 측정하지 않습니다. 정답 목록은 최대 5개를 선정한 참조이므로, 라벨 밖의 좋은 표현은 별도 검토 대상입니다. 라벨/판정 기준을 변경하면 버전을 고정하고 모든 모델을 같은 기준으로 다시 비교하세요.

## 저장·백업·검증

평가 기록과 검수 이력은 `runtime/paperlingo.sqlite3`에 저장합니다. 테스트는 임시 DB와 모의 LLM으로 실행하며 유료 API를 호출하지 않습니다.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

백업 시에는 서비스를 잠시 중지한 뒤 runtime 디렉터리 전체를 복사하거나 SQLite의 backup API를 사용합니다. 데이터셋 버전, 지표 버전, 평가 범위, 모델명, judge 프롬프트 해시, 사용 토큰, 지연 시간, 검수 이력은 결과 JSON에 남습니다. 서버 재시작으로 중단된 평가는 미완료 상태로 복구되며 이미 완료된 문항을 보존합니다.

주요 파일:

- `paperlingo/app.py`: HTTP API, 작업 실행, 공개/팀 접근
- `paperlingo/dataset.py`, `scoring.py`: 데이터 감사, 분할, 정확 매칭과 집계
- `paperlingo/judge.py`, `prompts/judge_v1.txt`: 블라인드 LLM 검수와 공통 rubric
- `paperlingo/static/`: 웹 화면
- `scripts/example_generator.py`: 초록만 입력하는 예제 생성기
- `scripts/local_judge_server.py`: 로컬 GPU 추론 서버
- `tests/`: 채점, 평가기, API, 예외 처리 테스트

구현 시 참고: [FastAPI 서버 실행](https://fastapi.tiangolo.com/deployment/manually/), [Pydantic 검증 모델](https://docs.pydantic.dev/latest/concepts/models/), [OpenAI 공식 JSON 출력 문서](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
