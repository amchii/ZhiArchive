import asyncio
from datetime import datetime, timedelta

from archive.config import default, settings
from archive.core import ActivityMonitor


async def main():
    monitor = ActivityMonitor(
        settings.people,
        settings.states_dir.joinpath(default.state_file),
        fetch_until=datetime.now() - timedelta(days=settings.monitor_fetch_until),
    )
    await monitor.run(headless=settings.monitor_headless)


if __name__ == "__main__":
    asyncio.run(main())
