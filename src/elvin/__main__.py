"""Run Elvin with Uvicorn."""

import uvicorn

from elvin.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "elvin.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
