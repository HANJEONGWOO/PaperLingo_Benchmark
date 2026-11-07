"""Server-only configuration. Credentials never enter API responses."""
from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    runtime_dir: Path = ROOT / "runtime"
    password: str = ""
    public_read: bool = False
    public_access: bool = False
    secure_cookie: bool = False
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]", "testserver")
    host: str = "127.0.0.1"
    port: int = 8000
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_model: str = ""
    judge_concurrency: int = 2
    judge_batch_size: int = 100
    judge_timeout: float = 90.0
    max_request_bytes: int = 16 * 1024 * 1024
    max_runs: int = 200
    session_hours: int = 12

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        config = cls(
            data_dir=Path(os.getenv("PAPERLINGO_DATA_DIR", str(ROOT / "data"))),
            runtime_dir=Path(os.getenv("PAPERLINGO_RUNTIME_DIR", str(ROOT / "runtime"))),
            password=os.getenv("PAPERLINGO_PASSWORD", ""),
            public_read=_bool("PAPERLINGO_PUBLIC_READ"),
            public_access=_bool("PAPERLINGO_PUBLIC_ACCESS"),
            secure_cookie=_bool("PAPERLINGO_SECURE_COOKIE"),
            allowed_hosts=tuple(x.strip() for x in os.getenv("PAPERLINGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",") if x.strip()),
            host=os.getenv("PAPERLINGO_HOST", "127.0.0.1"),
            port=int(os.getenv("PAPERLINGO_PORT", "8000")),
            judge_base_url=os.getenv("JUDGE_BASE_URL", "").rstrip("/"),
            judge_api_key=os.getenv("JUDGE_API_KEY", ""),
            judge_model=os.getenv("JUDGE_MODEL", ""),
            judge_concurrency=max(1, min(8, int(os.getenv("JUDGE_CONCURRENCY", "2")))),
            judge_batch_size=max(1, min(1000, int(os.getenv("JUDGE_BATCH_SIZE", "100")))),
            judge_timeout=float(os.getenv("JUDGE_TIMEOUT", "90")),
        )
        config.validate()
        return config

    def validate(self):
        if self.host not in {"localhost", "127.0.0.1", "::1"} and not self.password and not self.public_access:
            raise ValueError("외부 접속에는 PAPERLINGO_PASSWORD를 설정하세요.")
        if self.public_read and not self.password and not self.public_access:
            raise ValueError("공개 읽기 모드에도 관리자용 PAPERLINGO_PASSWORD가 필요합니다.")
        if self.judge_base_url:
            url = urlsplit(self.judge_base_url)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("JUDGE_BASE_URL에는 인증정보·쿼리가 없는 HTTP(S) API 주소를 사용하세요.")
        if not 0 < self.judge_timeout <= 300:
            raise ValueError("JUDGE_TIMEOUT은 0초 초과 300초 이하여야 합니다.")
        if self.judge_model and len(self.judge_model) > 200:
            raise ValueError("JUDGE_MODEL은 200자 이하여야 합니다.")

    @property
    def judge_configured(self):
        return bool(self.judge_base_url and self.judge_model)
