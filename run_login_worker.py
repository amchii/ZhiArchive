import asyncio

from archive.login import ZhiLogin


async def main():
    login = ZhiLogin(headless=True)
    await login.run()


if __name__ == "__main__":
    asyncio.run(main())
