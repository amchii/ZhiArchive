import asyncio


async def main():
    import run_archiver
    import run_login_worker
    import run_monitor

    await asyncio.gather(
        run_login_worker.main(), run_monitor.main(), run_archiver.main()
    )


if __name__ == "__main__":
    asyncio.run(main())
