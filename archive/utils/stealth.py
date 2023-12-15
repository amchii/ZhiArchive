from playwright.async_api import BrowserContext as AsyncBrowserContext
from playwright.sync_api import BrowserContext as SyncBrowserContext
from playwright_stealth import StealthConfig


async def stealth_async(context: AsyncBrowserContext, config: StealthConfig = None):
    """teaches asynchronous playwright Context to be stealthy like a ninja!"""
    for script in (config or StealthConfig()).enabled_scripts:
        await context.add_init_script(script)


def stealth_sync(context: SyncBrowserContext, config: StealthConfig = None):
    """teaches synchronous playwright Context to be stealthy like a ninja!"""
    for script in (config or StealthConfig()).enabled_scripts:
        context.add_init_script(script)
