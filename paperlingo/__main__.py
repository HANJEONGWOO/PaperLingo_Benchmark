import uvicorn
from .config import Settings
from .app import create_app


def main():
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, workers=1, proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1")


if __name__ == "__main__":
    main()
