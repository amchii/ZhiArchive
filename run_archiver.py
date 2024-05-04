import asyncio

from archive.config import default, settings
from archive.core.archiver import Archiver


def get_archiver():
    return Archiver(
        settings.people, settings.states_dir.joinpath(default.state_file), interval=1
    )


async def main():
    archiver = get_archiver()
    await archiver.run(headless=settings.archiver_headless)


if __name__ == "__main__":
    asyncio.run(main())
