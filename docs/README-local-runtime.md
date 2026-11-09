# 로컬 LLM 평가기

`scripts/local_judge_server.py`는 이미 내려받은 Qwen3-8B를 GPU에서 실행하는 선택 기능입니다. 외부 유료 API, API 키, 모델 자동 다운로드를 사용하지 않습니다. 웹 앱의 가벼운 `.venv`와 별도로 `.venv-llm`에서 실행합니다. 현재 구성은 **개발용 기준 평가기**이며 사람의 평가와 일치하는지 검증한 최종 평가 모델이 아닙니다.

## 요구 사항과 설치

- NVIDIA GPU: Qwen3-8B BF16 가중치 약 16GB와 문맥용 추가 메모리가 필요합니다. RTX 3090 24GB에서 검증합니다.
- Python 3.12, CUDA를 지원하는 PyTorch, Transformers 4.51 이상.
- `~/.cache/huggingface/hub`에 완전히 저장된 `Qwen/Qwen3-8B`, 또는 `--model-path`로 지정한 로컬 모델 디렉터리.
- `local_files_only=True`, `trust_remote_code=False`로 읽으므로 캐시가 없으면 시작에 실패합니다. 모델을 자동으로 내려받지 않습니다.

이 호스트의 기존 `ai_scientist` 환경은 CPU 전용 PyTorch이고 다른 일부 환경은 드라이버보다 최신 CUDA 빌드여서, 기존 환경을 변경하지 않는 별도 환경을 사용합니다.

```bash
python3 -m venv .venv-llm
.venv-llm/bin/python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
.venv-llm/bin/python -m pip install transformers==4.57.1
.venv-llm/bin/python scripts/local_judge_server.py
```

설치 명령은 추론 라이브러리를 다운로드합니다. 이후 서버의 모델 로딩과 추론은 오프라인으로 동작합니다. GPU가 작동하지 않으면 CPU로 조용히 전환하지 않고 시작 오류를 냅니다.

## 웹 앱 연결

현재 기본값은 `PAPERLINGO_REVIEW_MODE=code`이며 보기 검사에는 LLM이 필요하지 않습니다. 이전 LLM 검수를 다시 사용할 때만 웹 앱의 환경 변수에 다음을 설정합니다.

```dotenv
PAPERLINGO_REVIEW_MODE=llm
JUDGE_BASE_URL=http://127.0.0.1:11434/v1
JUDGE_MODEL=Qwen/Qwen3-8B
JUDGE_API_KEY=
JUDGE_TIMEOUT=300
JUDGE_CONCURRENCY=1
```

추론 서버는 `127.0.0.1:11434`에만 바인딩합니다. 외부 사용자는 공개된 웹 앱의 평가 버튼으로 사용하며, 추론 포트를 공유기에 추가로 개방하지 않습니다.

```bash
curl http://127.0.0.1:11434/health
curl http://127.0.0.1:11434/v1/models
```

`/health`의 `offline`, `device`, `dtype`, `gpu_allocated_gib`, `busy`로 실행 상태를 확인할 수 있습니다. 요청은 동시에 한 개만 처리합니다. 입력과 출력 합계는 8192토큰, 출력은 기본 3072토큰으로 제한합니다. 예제 생성기의 최대 5개 문항 JSON도 수용하기 위한 상한이며, 모델이 응답을 끝내면 일찍 종료합니다. 입력을 임의로 잘라내지 않으며 초과하면 오류를 반환합니다. `enable_thinking=False`, temperature 0을 고정합니다.

`response_format=json_object` 요청을 받지만 문법 제약을 강제하는 생성 엔진은 아닙니다. 모델이 JSON을 따르지 못하거나 출력이 잘리면 웹 평가기의 엄격한 검증에서 평가 오류가 되어 최종 점수가 확정되지 않습니다. 보기 품질을 판단하는 judge 호출에는 빈칸 문맥과 선택지만 제공하고, 빈칸 없는 원문·정답 키·정답 라벨은 포함하지 않습니다.

`llm` 모드에서 웹으로 결과를 제출하면 전체 문항을 자동 검수합니다. `JUDGE_BATCH_SIZE`는 내부 묶음 크기이며 이후 묶음도 자동 처리합니다. 모델의 불확실 판정은 근거와 함께 보수적으로 품질 0점을 기록하고, 사람 검수는 선택 사항으로 둡니다. 임시 만점 버튼은 실제 검수와 구분해 표시합니다. 웹 서버 재시작 후 GPU가 이전 추론을 마무리하는 경우, 알려진 로컬 `503 busy` 응답에 대해서만 `JUDGE_TIMEOUT` 한도 내에서 기다린 뒤 재시도합니다.

예제 생성기를 같은 모델에 연결하려면 `GENERATOR_BASE_URL=http://127.0.0.1:11434/v1`, `GENERATOR_MODEL=Qwen/Qwen3-8B`, `GENERATOR_TIMEOUT=300`을 설정합니다. 학습 표현을 생성하는 이 호출에는 선택한 초록이 필요하지만 정답 라벨은 읽지 않습니다. 평가 작업과 동시에 실행하면 사용 중이라는 오류를 받을 수 있으므로 순서대로 실행합니다.

## 사용자 서비스

저장소 경로가 예시와 같은 경우 다음으로 등록합니다. 다른 경로라면 service 파일의 WorkingDirectory와 ExecStart를 먼저 수정합니다.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/paperlingo-llm.service.example ~/.config/systemd/user/paperlingo-llm.service
systemctl --user daemon-reload
systemctl --user enable --now paperlingo-llm
systemctl --user status paperlingo-llm
journalctl --user -u paperlingo-llm -n 30
```

중지: `systemctl --user stop paperlingo-llm`. 서비스가 시작할 때 모델을 GPU에 올립니다. 사용자 서비스의 재부팅 자동 실행은 WSL 시작 및 사용자 systemd 세션 설정에도 영향을 받습니다.

## 검증과 한계

프로그램 테스트와 별도로 정상 문항, 복수 정답 문항을 실제 모델에 보내 응답 JSON과 판정 결과를 확인합니다. 소수 문항의 성공은 교육적 타당성이나 사람과의 일치율을 증명하지 않습니다. 최종 평가 전에는 사람이 검수한 표본으로 모델 판정의 신뢰도를 측정해야 합니다.

2026-11-07 이 호스트에서 RTX 3090으로 Qwen3-8B BF16을 실행했고 모델이 할당한 GPU 메모리는 약 15.3GiB였습니다. 웹 예제 2문항은 문항당 약 18~21초에 평가되어 JSON과 결과가 저장되었습니다. 이때 `consistent with`의 `with`를 틀렸다고 하고 `for`를 허용하는 오판도 발견했습니다. 기록은 `runtime/web-llm-smoke.json`에 있습니다. 이는 실제 실행과 저장 경로를 확인한 소규모 점검이며, 평가 모델의 전체 정확도나 신뢰도를 검증한 결과가 아닙니다.

공식 근거: [Qwen3-8B 모델 사용법 및 non-thinking 모드](https://huggingface.co/Qwen/Qwen3-8B), [PyTorch 버전별 CUDA 설치 명령](https://pytorch.org/get-started/previous-versions/).
