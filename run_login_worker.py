import asyncio

from archive.config import settings
from archive.core.login import ZhiLogin


async def main():
    login = ZhiLogin(headless=settings.login_worker_headless)
    await login.run()


if __name__ == "__main__":
    asyncio.run(main())
