# 동료 개발 서버와 공용 서버 배포

동료는 개인 Docker 서버에서 수정 사항을 확인하고 PR을 만듭니다. 현재 저장소의 기본 브랜치는 **`master`**입니다. GitHub Actions가 Python·브라우저 테스트와 컨테이너 채점을 검사합니다. 배포 설정을 완료한 공용 서버는 `master`의 검사를 통과한 커밋으로 갱신됩니다.

## 1. 동료의 PC에서 실행

Docker Desktop 또는 Docker Engine과 **Docker Compose 2.32.0 이상**을 설치합니다. Windows는 Docker Desktop의 Linux 컨테이너를 사용합니다.

```bash
git clone https://github.com/HANJEONGWOO/PaperLingo_Benchmark.git
cd PaperLingo_Benchmark
git switch -c feature/my-change
docker compose up --build --watch
```

`http://localhost:4173`을 엽니다. 기본 검수는 코드 검사이므로 GPU·LLM·API 키가 필요 없습니다. `.env`는 컨테이너에 복사하거나 읽어 넣지 않습니다. 공개 모드인 개발 서버는 호스트의 `127.0.0.1`에만 포트를 열어 해당 PC에서 접근합니다.

| 변경 | 반영 방식 |
| --- | --- |
| `paperlingo/`의 Python·HTML·CSS·JS | 저장 시 동기화 후 서버 재시작 |
| `prompts/`, 원본 초록·라벨·선정 프롬프트 | 저장 시 동기화 후 서버 재시작 |
| `requirements.txt`, `Dockerfile` | 저장 시 이미지 재빌드·컨테이너 재생성 |
| `compose.yaml`, 포트 환경변수 | 실행 명령을 종료한 뒤 다시 실행 |

브라우저 화면은 새로고침합니다. Watch가 켜져 있는 동안 서버가 잠시 재시작될 수 있습니다. Python 프로세스는 1개이며 파일을 변경할 때 평가 중인 요청이 끊길 수 있으므로 개발 서버에서 사용합니다.

4173이 사용 중이면 다른 포트로 실행합니다. 공용 서버가 실행 중인 PC에서는 이 방법을 사용하세요.

```bash
# Linux / macOS / WSL
PAPERLINGO_DEV_PORT=4174 docker compose up --build --watch
```

```powershell
# Windows PowerShell
$env:PAPERLINGO_DEV_PORT = "4174"
docker compose up --build --watch
```

이 경우 접속 주소는 `http://localhost:4174`입니다. 서로 다른 작업 폴더를 동시에 실행하려면 포트와 Compose 프로젝트 이름을 모두 다르게 지정합니다.

```bash
PAPERLINGO_DEV_PORT=4175 docker compose -p paperlingo-feature-b up --build --watch
```

평가 기록은 `<프로젝트 이름>_dev-runtime` 볼륨에 저장됩니다. `docker compose down`이나 재빌드 후에도 남으며 공용 서버의 SQLite와 연결되지 않습니다. **`docker compose down --volumes`는 해당 개발 기록을 삭제합니다.** 컨테이너 안의 임시 파일 변경은 재빌드할 때 사라지므로 코드는 호스트에서 편집하세요.

## 2. PR과 자동 검사

```bash
git add <수정한 파일>
git commit -m "feat: 변경 설명"
git push -u origin feature/my-change
```

GitHub에서 `master`를 대상으로 PR을 만듭니다. `.github/workflows/ci.yml`은 다음 두 검사를 GitHub의 Ubuntu 서버에서 실행합니다.

- **Python and browser tests**: Python 3.12, 전체 pytest, Chromium을 이용한 실제 화면 검사.
- **Container and persistence**: Docker 빌드·기동, 예제 100점 채점, 컨테이너 재생성 후 같은 평가 기록 조회.

컨테이너 검사에서는 `docker/setup-compose-action`으로 Docker Compose **v5.1.4**를 설치합니다. GitHub 실행 환경의 기본 버전이 개발 설정의 `initial_sync`를 지원하지 않는 경우에도 같은 버전으로 검증합니다. `additional properties 'initial_sync' not allowed` 오류가 있는 과거 실행은 수정 커밋을 push한 뒤 새 실행에서 확인하세요.

PR에서는 공용 서버 배포를 실행하지 않습니다. `master`에 push된 커밋에서 두 검사 모두 성공해야 배포 작업을 시작합니다. 기본 브랜치 이름을 바꾸면 workflow의 브랜치 필터·조건·`ls-remote` 대상도 함께 바꾸세요.

저장소의 **Settings → Rules → Rulesets**에서 `master`를 대상으로 PR과 두 검사를 필수로 지정하면 동료들이 검사를 거쳐 병합하도록 강제할 수 있습니다. 이 저장소 설정은 YAML 파일을 추가하는 것만으로 활성화되지 않습니다.

## 3. 공용 서버에서 한 번 설정

공용 웹 서버는 기존 사용자 systemd 서비스 `paperlingo-web`를 유지합니다. 개발용 Docker와 달리 배포는 **같은 Linux 사용자**의 별도 Python 가상환경으로 실행합니다. 기존 DDNS·4173 포트 연결은 유지하며 SSH 포트를 추가로 열 필요가 없습니다.

필요 조건은 Python 3.12+(`venv` 포함), Git, 실행 중인 사용자 systemd 서비스, GitHub·Python 패키지 저장소로 나가는 네트워크 연결입니다. GitHub에 올라가 테스트된 커밋만 배포합니다. 로컬 수정 내용은 배포에 포함되지 않습니다.

### 서버 경로 설정

```bash
mkdir -p ~/.config/paperlingo
install -m 600 deploy/server.example.json ~/.config/paperlingo/deploy.json
```

`~/.config/paperlingo/deploy.json`의 경로를 확인합니다. 예제는 현재 PC의 `~/git/PaperLingo_Benchmark/.env`, `~/git/PaperLingo_Benchmark/runtime`을 그대로 사용합니다. 파일에 API 키나 비밀번호를 넣지 않습니다. `service`와 `health_url`도 현재 서비스에 맞아야 합니다.

커밋된 깨끗한 작업 폴더에서 다음 검사를 실행할 수 있습니다. 서비스는 변경하지 않습니다.

```bash
python3 scripts/deploy_release.py --revision "$(git rev-parse HEAD)" --check
```

### GitHub Runner 등록

GitHub의 [이 저장소 Runner 설정](https://github.com/HANJEONGWOO/PaperLingo_Benchmark/settings/actions/runners)에서 **New self-hosted runner → Linux → x64**를 선택합니다. 다음 폴더에서 GitHub 화면에 표시된 다운로드·압축 해제·`config.sh` 명령을 실행합니다.

```bash
mkdir -p ~/.local/share/paperlingo-runner
cd ~/.local/share/paperlingo-runner
# GitHub 화면의 최신 다운로드·압축 해제 명령 실행
# config.sh 등록 명령에 --labels paperlingo-deploy 추가
```

등록할 저장소는 `https://github.com/HANJEONGWOO/PaperLingo_Benchmark`이며, 추가 라벨은 **`paperlingo-deploy`**입니다. Runner 등록 토큰은 GitHub 화면에서 복사해 해당 터미널에서만 사용하고 커밋·채팅에 넣지 않습니다. 등록은 저장소 관리자 권한이 필요합니다.

등록 후 저장소 폴더로 돌아와 같은 사용자 systemd에 연결합니다.

```bash
cd ~/git/PaperLingo_Benchmark
mkdir -p ~/.config/systemd/user
cp deploy/paperlingo-runner.service.example ~/.config/systemd/user/paperlingo-runner.service
systemctl --user daemon-reload
systemctl --user enable --now paperlingo-runner
systemctl --user status paperlingo-runner
```

GitHub에서 Runner가 **Idle**인지 확인합니다. 공개 저장소이므로 PR 검사는 GitHub 호스팅 서버에 두고, 운영 Runner를 PR의 `runs-on`에 사용하지 마세요. Runner 라벨 자체는 접근 권한 제한이 아닙니다. 저장소 쓰기 권한과 외부 기여자 workflow 실행 승인은 신뢰하는 팀원이 관리해야 합니다.

### 자동 배포 활성화

1. GitHub **Settings → Environments → production**에서 배포 가능한 브랜치를 `master`로 제한합니다.
2. **Settings → Secrets and variables → Actions → Variables**에 저장소 변수 `PAPERLINGO_AUTO_DEPLOY`를 만들고 값으로 `true`를 입력합니다. Secret이 아닌 **Variable**입니다.
3. 이 구성 파일들을 커밋·push합니다. 이후 `master`의 테스트가 통과하면 배포가 실행됩니다. 변수 설정 전에 끝난 실행은 자동으로 재시작하지 않으므로, 최신 `master` push 실행의 **Re-run all jobs**를 사용하거나 다음 변경을 병합합니다. `Run workflow` 수동 실행은 검사만 수행합니다.

Runner가 등록되지 않았거나 변수가 `true`가 아니면 자동 배포는 활성화되지 않습니다. 변수를 먼저 켜면 Runner 등록 전까지 배포 작업이 대기할 수 있으므로 등록 후 활성화하세요. 자동 배포를 중지하려면 변수 값을 `false`로 바꿉니다. 현재 서비스는 계속 실행됩니다.

WSL이 중지되면 웹 서비스와 Runner도 중지됩니다. PC를 켠 뒤 WSL을 실행해야 하며, WSL IP가 바뀌면 기존 Windows 포트 연결도 갱신해야 합니다.

## 4. 배포와 복구 방식

`scripts/deploy_release.py`는 다음 순서로 동작합니다.

1. 테스트한 SHA와 checkout이 같은지 확인하고, 변경된 추적 파일이 있으면 중단합니다. workflow는 이미 더 최신 `master`가 있으면 이전 커밋의 배포를 건너뜁니다.
2. `~/.local/share/paperlingo-deploy/releases/`에 해당 커밋의 앱·데이터·프롬프트를 추출하고 전용 가상환경에 패키지를 설치합니다. 기존 개발 작업 폴더에는 `git pull`이나 파일 덮어쓰기를 하지 않습니다.
3. 현재 실행 중인 버전보다 낮은 버전이면 중단합니다. 버전은 `paperlingo/app.py`의 `VERSION`을 사용합니다. 데이터와 앱을 임시 DB로 로드해 시작 가능한지 확인합니다. 여기까지 기존 웹 서비스는 계속 실행됩니다.
4. 서비스를 잠시 중지하고 SQLite backup API로 기록을 백업합니다. 사용자 서비스에 `90-paperlingo-deploy.conf` override를 추가해 새 릴리스로 시작합니다.
5. 새 프로세스와 `/api/health`가 정상인지 확인합니다. 실패하면 이전 override를 복원하고 이전 코드로 재시작합니다. 동시 배포는 잠금으로 차단합니다.

배포는 짧은 중단이 있는 방식입니다. 평가 중인 요청이 있으면 다시 제출해야 할 수 있습니다. `.env`와 `runtime`은 기존 위치를 계속 사용합니다. 자동 배포 활성화 이후에는 개발 작업 폴더에서 파일을 바꾸고 `systemctl restart`만 해도 반영되던 방식 대신, `master`에 병합해 배포해야 합니다.

DB를 변경하는 새 코드가 시작된 뒤 문제가 발생하면 코드만 복원됩니다. **배포 후 새 제출을 잃지 않도록 DB 백업을 자동으로 덮어쓰지는 않습니다.** DB 스키마 변경은 이전 코드와의 호환성을 확인하고 배포하세요. 백업과 이전 릴리스는 자동 삭제하지 않으므로 운영자가 보관 기간을 정해 정리할 수 있습니다.

```bash
cat ~/.local/share/paperlingo-deploy/current.json
systemctl --user status paperlingo-web
journalctl --user -u paperlingo-web -n 80 --no-pager
journalctl --user -u paperlingo-runner -n 80 --no-pager
```

`backups/<시각>/previous-override.json`에 이전 서비스 설정이 기록되고, 같은 폴더에 `paperlingo.sqlite3` 백업이 있습니다. 수동 코드 복구는 해당 JSON의 `path`에 `content`를 복원한 뒤 `daemon-reload`와 서비스 재시작으로 수행합니다. `content`가 `null`이면 해당 override 파일만 제거해 원래 서비스 설정으로 돌아갑니다. DB 복구가 필요한 경우에는 서비스를 중지하고 현재 DB·WAL·SHM 파일을 별도 보관한 뒤 백업을 복원하세요.

참고: [Docker Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/), [Compose Develop](https://docs.docker.com/reference/compose-file/develop/), [GitHub Runner 등록](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).
