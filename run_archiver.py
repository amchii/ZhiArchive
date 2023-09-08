import asyncio

from archive.config import default, settings
from archive.core import Archiver


async def main():
    archiver = Archiver(
        settings.people, settings.states_dir.joinpath(default.state_file), interval=1
    )
    await archiver.run(headless=settings.archiver_headless)


if __name__ == "__main__":
    asyncio.run(main())
