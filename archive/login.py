from playwright.async_api import Playwright

from .config import default, settings
from .core import init_context


async def login(playwright: Playwright, qrcode_path="login_qrcode.png") -> None:
    browser = await playwright.chromium.launch(headless=False)
    context = await browser.new_context()
    await init_context(context)
    page = await context.new_page()
    await page.goto("https://www.zhihu.com/signin?next=%2F")
    await page.pause()
    await context.storage_state(path=settings.states_dir.joinpath(default.state_file))
    # ---------------------
    await context.close()
    await browser.close()
