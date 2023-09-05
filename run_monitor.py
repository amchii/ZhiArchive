import asyncio
from datetime import datetime

from archive.config import default, settings
from archive.core import ActivityMonitor


async def main():
    monitor = ActivityMonitor(
        settings.people,
        settings.states_dir.joinpath(default.state_file),
        fetch_until=datetime.now(),
    )
    await monitor.run(headless=settings.monitor_headless)


if __name__ == "__main__":
    asyncio.run(main())
