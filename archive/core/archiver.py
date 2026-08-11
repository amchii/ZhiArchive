import asyncio
import html
import json
import pathlib
import re
from datetime import datetime
from functools import partial
from typing import Literal, TypedDict
from urllib import parse

import aiofiles
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Playwright, Route, async_playwright
from playwright_stealth import Stealth

from archive.config import default, settings
from archive.core.base import (
    ActivityItem,
    ActivityMeta,
    ArchiveTask,
    BaseWorker,
    Cfg,
    Target,
    TargetType,
    WorkStatus,
)
from archive.storage import SQLiteStore
from archive.utils.common import (
    dt_fromisoformat,
    get_validate_filename,
    uuid_hex,
)
from archive.utils.encoder import JSONEncoder
from archive.utils.js import get_page_scrollHeight, get_page_scrollWidth

ANSWER_PATH_PATTERN = re.compile(r"^/question/\d+/answer/\d+/?$")
ARTICLE_PATH_PATTERN = re.compile(r"^/p/\d+/?$")


class TextArchive(TypedDict):
    """表示从知乎页面抽取出的文本归档内容。"""

    title: str
    url: str
    author: str
    author_url: str
    published_at: str
    updated_at: str
    target_type: str
    html: str
    markdown: str


class ArchiveResult(TypedDict):
    """表示一次归档成功后的 results 相对目录和文件名。"""

    archive_path: str
    files: dict[str, str]


def format_text_archive_html(archive: TextArchive) -> str:
    """
    将抽取出的正文包装为可独立阅读的 HTML 文件。

    Args:
        archive: 从知乎回答或文章页面抽取出的正文和元数据。
    """
    title = html.escape(archive["title"])
    author = html.escape(archive["author"] or "未知作者")
    source_url = html.escape(archive["url"], quote=True)
    author_url = html.escape(archive["author_url"], quote=True)
    published_at = html.escape(archive["published_at"])
    updated_at = html.escape(archive["updated_at"])
    target_type = html.escape(archive["target_type"])
    byline = (
        f'<a href="{author_url}" rel="noreferrer">{author}</a>'
        if author_url
        else author
    )
    time_parts = []
    if published_at:
        time_parts.append(f"发布于 {published_at}")
    if updated_at and updated_at != published_at:
        time_parts.append(f"更新于 {updated_at}")
    time_text = " · ".join(time_parts)
    meta_line = f"{target_type} · {byline}"
    if time_text:
        meta_line = f"{meta_line} · {time_text}"

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta
    http-equiv="Content-Security-Policy"
    content="default-src 'none'; img-src https: data: file:; style-src 'unsafe-inline';"
  >
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.72;
      color: #1f2329;
      background: #f6f7f9;
    }}
    body {{
      margin: 0;
      padding: 40px 16px;
    }}
    .zhi-archive {{
      box-sizing: border-box;
      max-width: 760px;
      margin: 0 auto;
      padding: 40px 48px;
      background: #fff;
      border: 1px solid #e5e7eb;
    }}
    .zhi-archive-header {{
      margin-bottom: 32px;
      padding-bottom: 20px;
      border-bottom: 1px solid #e5e7eb;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 30px;
      line-height: 1.32;
    }}
    .zhi-archive-meta {{
      margin: 0;
      color: #6b7280;
      font-size: 14px;
    }}
    .zhi-archive-source {{
      margin: 8px 0 0;
      color: #6b7280;
      font-size: 14px;
      word-break: break-all;
    }}
    .zhi-archive-content p {{
      margin: 1em 0;
    }}
    .zhi-archive-content a {{
      color: #175199;
      text-decoration: none;
    }}
    .zhi-archive-content a:hover {{
      text-decoration: underline;
    }}
    .zhi-archive-content img {{
      max-width: 100%;
      height: auto;
      display: block;
      margin: 12px auto;
    }}
    .zhi-archive-content figure {{
      margin: 24px 0;
    }}
    .zhi-archive-content figcaption {{
      margin-top: 8px;
      color: #6b7280;
      font-size: 14px;
      text-align: center;
    }}
    .zhi-archive-content blockquote {{
      margin: 1em 0;
      padding-left: 1em;
      border-left: 4px solid #d0d7de;
      color: #57606a;
    }}
    .zhi-archive-content pre {{
      overflow: auto;
      padding: 16px;
      background: #f6f8fa;
      border-radius: 6px;
    }}
    .zhi-archive-content code {{
      font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    }}
    .zhi-archive-content table {{
      width: 100%;
      border-collapse: collapse;
      margin: 1em 0;
    }}
    .zhi-archive-content th,
    .zhi-archive-content td {{
      border: 1px solid #d0d7de;
      padding: 6px 10px;
    }}
    @media (max-width: 640px) {{
      body {{
        padding: 0;
      }}
      .zhi-archive {{
        padding: 24px 18px;
        border: 0;
      }}
      h1 {{
        font-size: 24px;
      }}
    }}
  </style>
</head>
<body>
  <article class="zhi-archive">
    <header class="zhi-archive-header">
      <h1>{title}</h1>
      <p class="zhi-archive-meta">{meta_line}</p>
      <p class="zhi-archive-source">
        来源：<a href="{source_url}" rel="noreferrer">{source_url}</a>
      </p>
    </header>
    <main class="zhi-archive-content">
{archive["html"]}
    </main>
  </article>
</body>
</html>
"""


def format_text_archive_markdown(archive: TextArchive) -> str:
    """
    将抽取出的正文包装为 Markdown 文件。

    Args:
        archive: 从知乎回答或文章页面抽取出的正文和元数据。
    """
    lines = [
        f"# {archive['title']}",
        "",
        f"- 类型：{archive['target_type']}",
        f"- 作者：{archive['author'] or '未知作者'}",
        f"- 来源：{archive['url']}",
    ]
    if archive["published_at"]:
        lines.append(f"- 发布时间：{archive['published_at']}")
    if archive["updated_at"] and archive["updated_at"] != archive["published_at"]:
        lines.append(f"- 更新时间：{archive['updated_at']}")
    lines.extend(["", "---", "", archive["markdown"].strip(), ""])
    return "\n".join(lines)


def parse_archive_url(url: str) -> tuple[str, TargetType]:
    """
    校验并标准化可由 archiver 保存的知乎链接。

    Args:
        url: 知乎回答或专栏文章链接。

    Returns:
        标准化后的 HTTPS 链接及目标类型。

    Raises:
        ValueError: 链接不是受支持的知乎回答或专栏文章。
    """
    value = url.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    elif value.startswith("/question/"):
        value = f"https://www.zhihu.com{value}"

    parsed = parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("请输入完整的知乎回答或文章链接")
    if parsed.username or parsed.password:
        raise ValueError("链接中不能包含用户认证信息")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("链接端口格式不正确") from error
    if port not in {None, 80, 443}:
        raise ValueError("链接不能包含自定义端口")

    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    if host in {"zhihu.com", "www.zhihu.com"} and ANSWER_PATH_PATTERN.fullmatch(
        parsed.path
    ):
        target_type = TargetType.ANSWER
        host = "www.zhihu.com"
    elif host == "zhuanlan.zhihu.com" and ARTICLE_PATH_PATTERN.fullmatch(parsed.path):
        target_type = TargetType.ARTICLE
    else:
        raise ValueError("仅支持知乎回答或专栏文章链接")

    return parse.urlunparse(("https", host, path, "", "", "")), target_type


class ZhihuContentWorker(BaseWorker):
    """封装知乎回答和文章的页面访问、元数据补全及正文抽取能力。"""

    name = "zhihu_content"

    async def referrer_route(
        self,
        route: Route,
        people: str | None = None,
    ) -> None:
        """为知乎目标请求补充指定用户页作为 Referer。

        Args:
            route: Playwright 拦截到的目标页面请求。
            people: 任务 payload 中的用户；Reader 未指定时使用自身配置。
        """
        headers = route.request.headers
        headers["Referer"] = (
            default.person_page_url.format(people=people)
            if people is not None
            else self.person_page_url
        )
        await route.continue_(headers=headers)

    async def extract_text_archive(
        self,
        page: Page,
        target: Target,
        target_type: TargetType,
        url: str,
    ) -> TextArchive:
        """
        从当前知乎回答或文章页面抽取 HTML 与 Markdown 正文。

        Args:
            page: 已打开目标页面的 Playwright 页面。
            target: 当前归档目标的元数据。
            target_type: 回答或文章类型。
            url: 标准化后的目标页面链接。
        """
        extracted = await page.evaluate(
            """
            ({ targetType, title, url }) => {
              const asAbsoluteUrl = (value) => {
                if (!value) {
                  return "";
                }
                try {
                  return new URL(value, location.href).href;
                } catch {
                  return value;
                }
              };

              const normalizeZhihuLink = (value) => {
                const absoluteUrl = asAbsoluteUrl(value);
                if (!absoluteUrl) {
                  return "";
                }
                try {
                  const parsed = new URL(absoluteUrl);
                  if (
                    parsed.hostname === "link.zhihu.com" &&
                    parsed.searchParams.has("target")
                  ) {
                    return parsed.searchParams.get("target") || absoluteUrl;
                  }
                } catch {
                  return absoluteUrl;
                }
                return absoluteUrl;
              };

              const findRoot = () => {
                if (targetType === "回答") {
                  const answerId = location.pathname.match(/answer\\/(\\d+)/)?.[1];
                  if (answerId) {
                    const answer = document.querySelector(
                      `.AnswerItem[name="${answerId}"]`
                    );
                    if (answer) {
                      return answer;
                    }
                  }
                  return (
                    document.querySelector(".AnswerItem[itemprop='mainEntityOfPage']") ||
                    document.querySelector(".AnswerItem")
                  );
                }
                return (
                  document.querySelector("article.Post-Main") ||
                  document.querySelector(".Post-Main")
                );
              };

              const originalRoot = findRoot();
              if (!originalRoot) {
                throw new Error("Cannot find Zhihu content root");
              }
              const originalRichText =
                originalRoot.querySelector(".RichText.ztext") || originalRoot;
              const cloned = originalRichText.cloneNode(true);

              cloned
                .querySelectorAll(
                  [
                    "script",
                    "style",
                    "button",
                    "svg",
                    ".ContentItem-actions",
                    ".RichContent-actions",
                    ".CornerButtons",
                  ].join(",")
                )
                .forEach((element) => element.remove());

              cloned.querySelectorAll("noscript").forEach((element) => {
                const parent = element.parentElement;
                if (parent && parent.querySelector("img")) {
                  element.remove();
                }
              });

              cloned.querySelectorAll("*").forEach((element) => {
                Array.from(element.attributes).forEach((attribute) => {
                  const name = attribute.name.toLowerCase();
                  if (
                    name.startsWith("on") ||
                    name === "class" ||
                    name === "style" ||
                    name.startsWith("data-za") ||
                    name === "contenteditable"
                  ) {
                    element.removeAttribute(attribute.name);
                  }
                });
              });

              cloned.querySelectorAll("a").forEach((link) => {
                const href = normalizeZhihuLink(link.getAttribute("href"));
                if (href) {
                  link.setAttribute("href", href);
                  link.setAttribute("rel", "noreferrer");
                } else {
                  link.removeAttribute("href");
                }
                link.removeAttribute("target");
              });

              cloned.querySelectorAll("img").forEach((image) => {
                const imageUrl = asAbsoluteUrl(
                  image.getAttribute("data-original") ||
                    image.getAttribute("src")
                );
                if (imageUrl) {
                  image.setAttribute("src", imageUrl);
                }
                image.removeAttribute("srcset");
                image.removeAttribute("data-original");
                image.removeAttribute("loading");
                if (!image.getAttribute("alt")) {
                  image.setAttribute("alt", "");
                }
              });

              cloned.querySelectorAll("p, li, figcaption").forEach((element) => {
                element.innerHTML = element.innerHTML.replace(/\\u200b/g, "");
              });

              const textOf = (node) =>
                (node?.textContent || "").replace(/\\u200b/g, "").trim();

              const escapeMarkdown = (value) =>
                value.replace(/([\\\\`*_{}\\[\\]()#+\\-.!|>])/g, "\\\\$1");

              const renderInline = (node) => {
                if (node.nodeType === Node.TEXT_NODE) {
                  return (node.textContent || "").replace(/\\u200b/g, "");
                }
                if (node.nodeType !== Node.ELEMENT_NODE) {
                  return "";
                }
                const tagName = node.tagName.toLowerCase();
                if (tagName === "br") {
                  return "\\n";
                }
                if (tagName === "img") {
                  const src = node.getAttribute("src") || "";
                  const alt = escapeMarkdown(node.getAttribute("alt") || "");
                  return src ? `![${alt}](${src})` : "";
                }
                const children = Array.from(node.childNodes)
                  .map(renderInline)
                  .join("");
                if (tagName === "strong" || tagName === "b") {
                  return children.trim() ? `**${children}**` : "";
                }
                if (tagName === "em" || tagName === "i") {
                  return children.trim() ? `*${children}*` : "";
                }
                if (tagName === "code") {
                  return children.trim() ? `\\`${children}\\`` : "";
                }
                if (tagName === "a") {
                  const href = node.getAttribute("href") || "";
                  const text = children.trim() || href;
                  return href ? `[${text}](${href})` : text;
                }
                if (tagName === "svg" || tagName === "button") {
                  return "";
                }
                return children;
              };

              const renderBlock = (node, depth = 0, index = 1) => {
                if (node.nodeType === Node.TEXT_NODE) {
                  return (node.textContent || "").trim();
                }
                if (node.nodeType !== Node.ELEMENT_NODE) {
                  return "";
                }
                const tagName = node.tagName.toLowerCase();
                if (tagName === "p") {
                  const content = renderInline(node).trim();
                  return content ? `${content}\\n\\n` : "";
                }
                if (/^h[1-6]$/.test(tagName)) {
                  const level = Number(tagName.slice(1));
                  const content = renderInline(node).trim();
                  return content ? `${"#".repeat(level)} ${content}\\n\\n` : "";
                }
                if (tagName === "figure") {
                  const images = Array.from(node.querySelectorAll("img"))
                    .map(renderInline)
                    .filter(Boolean)
                    .join("\\n");
                  const caption = textOf(node.querySelector("figcaption"));
                  const parts = [];
                  if (images) {
                    parts.push(images);
                  }
                  if (caption) {
                    parts.push(`*${caption}*`);
                  }
                  return parts.length ? `${parts.join("\\n")}\\n\\n` : "";
                }
                if (tagName === "blockquote") {
                  const content = renderChildren(node, depth)
                    .trim()
                    .split("\\n")
                    .map((line) => (line ? `> ${line}` : ">"))
                    .join("\\n");
                  return content ? `${content}\\n\\n` : "";
                }
                if (tagName === "pre") {
                  return `\\n\\`\\`\\`\\n${textOf(node)}\\n\\`\\`\\`\\n\\n`;
                }
                if (tagName === "ul" || tagName === "ol") {
                  return (
                    Array.from(node.children)
                      .filter((child) => child.tagName.toLowerCase() === "li")
                      .map((child, childIndex) =>
                        renderBlock(child, depth + 1, childIndex + 1)
                      )
                      .join("") + "\\n"
                  );
                }
                if (tagName === "li") {
                  const marker =
                    node.parentElement?.tagName.toLowerCase() === "ol"
                      ? `${index}.`
                      : "-";
                  const indent = "  ".repeat(Math.max(0, depth - 1));
                  const content = Array.from(node.childNodes)
                    .map((child) => {
                      if (
                        child.nodeType === Node.ELEMENT_NODE &&
                        ["ul", "ol"].includes(child.tagName.toLowerCase())
                      ) {
                        return `\\n${renderBlock(child, depth)}`;
                      }
                      return renderInline(child);
                    })
                    .join("")
                    .trim();
                  return content ? `${indent}${marker} ${content}\\n` : "";
                }
                if (tagName === "table") {
                  return `${node.outerHTML}\\n\\n`;
                }
                return renderChildren(node, depth);
              };

              const renderChildren = (node, depth = 0) =>
                Array.from(node.childNodes)
                  .map((child) => renderBlock(child, depth))
                  .join("");

              const author =
                originalRoot
                  .querySelector('meta[itemprop="name"]')
                  ?.getAttribute("content") || "";
              const authorLink = normalizeZhihuLink(
                originalRoot.querySelector("a.UserLink-link")?.getAttribute("href")
              );
              const publishedAt =
                originalRoot
                  .querySelector(
                    'meta[itemprop="dateCreated"], meta[itemprop="datePublished"]'
                  )
                  ?.getAttribute("content") || "";
              const updatedAt =
                originalRoot
                  .querySelector('meta[itemprop="dateModified"]')
                  ?.getAttribute("content") || "";

              return {
                title,
                url,
                author,
                author_url: authorLink,
                published_at: publishedAt,
                updated_at: updatedAt,
                target_type: targetType,
                html: cloned.innerHTML.trim(),
                markdown: renderChildren(cloned).replace(/\\n{3,}/g, "\\n\\n").trim(),
              };
            }
            """,
            {
                "targetType": target_type.value,
                "title": target["title"],
                "url": url,
            },
        )
        archive = TextArchive(**extracted)
        if not archive["author"]:
            archive["author"] = target["author"]
        return archive

    async def fill_target_metadata(
        self,
        page: Page,
        target: Target,
        target_type: TargetType,
    ) -> None:
        """为知乎回答或文章补全页面标题和作者。

        Args:
            page: 已打开目标页面的 Playwright 页面。
            target: 待补全的目标数据。
            target_type: 回答或文章类型。
        """
        if not target["title"]:
            try:
                title = await page.locator('meta[property="og:title"]').get_attribute(
                    "content", timeout=1000
                )
            except PlaywrightError:
                title = ""
            if not title:
                title = await page.title()
            target["title"] = title.removesuffix(" - 知乎").strip()

        if target["author"]:
            return
        if target_type == TargetType.ANSWER:
            author_selector = "div.AnswerCard a.UserLink-link"
        else:
            author_selector = (
                "div.Post-Author a.UserLink-link, div.AuthorInfo a.UserLink-link"
            )
        try:
            author_href = await page.locator(author_selector).first.get_attribute(
                "href", timeout=1000
            )
        except PlaywrightError:
            author_href = ""
        if author_href:
            target["author"] = author_href.rstrip("/").rsplit("/", maxsplit=1)[-1]


class ArchiveQueueService:
    """构造手动归档任务并写入 SQLite，不修改运行中的 Archiver。"""

    def __init__(
        self,
        store: SQLiteStore,
        wakeup_event: asyncio.Event | None = None,
    ) -> None:
        """创建归档入队服务。

        Args:
            store: 主服务持有的 SQLite store。
            wakeup_event: 新任务写入后用于唤醒 Archiver 的事件。
        """
        self.store = store
        self.wakeup_event = wakeup_event

    async def enqueue_url(self, url: str) -> tuple[ArchiveTask, ActivityItem]:
        """把知乎回答或文章链接加入持久化归档队列。

        Args:
            url: 知乎回答或专栏文章链接。

        Returns:
            已写入的任务及其动态数据。
        """
        normalized_url, target_type = parse_archive_url(url)
        global_config = await self.store.get_settings("global")
        people = str(global_config.get("people") or settings.people).strip()
        if not people:
            raise ValueError("目标用户 people 尚未配置")
        now = datetime.now()
        item_id = uuid_hex()
        item = ActivityItem(
            id=item_id,
            target=Target(
                title="",
                link=normalized_url,
                author="",
                fetched_at=now,
            ),
            meta=ActivityMeta(
                action="手动归档",
                target_type=target_type,
                acted_at=now,
                raw=[normalized_url],
            ),
            people=people,
        )
        task = ArchiveTask(item_id, payload=item)
        inserted = await self.store.enqueue_archive_item(item)
        if not inserted:
            raise RuntimeError("手动归档任务 ID 冲突，请重试")
        if self.wakeup_event is not None:
            self.wakeup_event.set()
        return task, item


class Archiver(ZhihuContentWorker):
    """消费归档队列并保存知乎内容、元数据及截图。"""

    name = "archiver"
    output_name = "archives"
    configurable = BaseWorker.configurable + [
        Cfg(
            "screenshot_max_page_scroll_height",
        ),
        Cfg("save_type"),
    ]

    def __init__(self, *args: object, **kwargs: object) -> None:
        """创建归档 worker 并初始化截图配置。"""
        super().__init__(*args, **kwargs)
        self.screenshot_max_page_scroll_height = (
            settings.screenshot_max_page_scroll_height
        )
        self.save_type: Literal["jpeg", "png"] = "jpeg"

    async def prepare_page_for_screenshot(self, page: Page) -> None:
        """清理知乎页面中会干扰长截图拼接的浮动元素。"""
        await page.evaluate(
            """
            () => {
              const styleId = "zhi-archive-screenshot-style";
              let screenshotStyle = document.getElementById(styleId);
              if (!screenshotStyle) {
                screenshotStyle = document.createElement("style");
                screenshotStyle.id = styleId;
                (document.head || document.documentElement).appendChild(
                  screenshotStyle
                );
              }
              screenshotStyle.textContent = `
                .ContentItem-actions.is-fixed,
                .RichContent-actions.is-fixed,
                .CornerButtons {
                  display: none !important;
                }
              `;

              const hideElement = (element) => {
                if (!element) {
                  return;
                }
                element.setAttribute("data-zhi-archive-hidden", "true");
                element.style.setProperty("display", "none", "important");
              };

              const findFixedAncestor = (element) => {
                let current = element;
                while (current && current !== document.body) {
                  const style = window.getComputedStyle(current);
                  if (style.position === "fixed") {
                    return current;
                  }
                  current = current.parentElement;
                }
                return null;
              };

              const appHeader = document.querySelector(".AppHeader");
              hideElement(findFixedAncestor(appHeader));

              document
                .querySelectorAll(
                  ".ContentItem-actions.is-fixed, " +
                  ".RichContent-actions.is-fixed, " +
                  ".CornerButtons"
                )
                .forEach(hideElement);
            }
            """
        )
        await page.wait_for_timeout(timeout=200)

    async def save_text_archive(
        self,
        target_dir: pathlib.Path,
        title: str,
        archive: TextArchive,
    ) -> dict[str, str]:
        """
        将 HTML 与 Markdown 文本归档写入目标目录。

        Args:
            target_dir: 当前归档对象的保存目录。
            title: 已清理过的归档文件名前缀。
            archive: 从页面抽取出的正文和元数据。
        """
        html_filename = f"{title}.html"
        markdown_filename = f"{title}.md"
        html_path = target_dir.joinpath(html_filename)
        markdown_path = target_dir.joinpath(markdown_filename)
        async with aiofiles.open(html_path, "w", encoding="utf-8") as fp:
            await fp.write(format_text_archive_html(archive))
        async with aiofiles.open(markdown_path, "w", encoding="utf-8") as fp:
            await fp.write(format_text_archive_markdown(archive))
        return {
            "html": html_filename,
            "markdown": markdown_filename,
        }

    async def store_one(
        self,
        item: ActivityItem,
        page: Page,
    ) -> ArchiveResult | None:
        """
        打开并保存一个回答或文章归档。

        Args:
            item: 待归档的动态数据。
            page: 用于访问目标链接的 Playwright 页面。
        """
        people = str(item.get("people") or "").strip()
        if not people:
            raise ValueError("归档任务缺少 people，无法确定保存目录")
        target = item["target"]
        meta = item["meta"]
        if not target["link"]:
            return
        url, target_type = parse_archive_url(target["link"])
        await page.route(url, partial(self.referrer_route, people=people))
        await self.goto(page, url)
        await self.fill_target_metadata(page, target, target_type)
        if not target["title"]:
            target["title"] = f"{target_type.value}-{item['id'][:8]}"
        # 确保页面中的图片被加载
        if target_type == TargetType.ANSWER:
            imgs_locator = page.locator("div.AnswerCard figure img")
        else:
            imgs_locator = page.locator("div.Post-RichTextContainer figure img")
        for i in range(await imgs_locator.count()):
            img_locator = imgs_locator.nth(i)
            await img_locator.scroll_into_view_if_needed()
        await page.wait_for_timeout(timeout=500)
        await self.prepare_page_for_screenshot(page)

        now = datetime.now()
        acted_at = dt_fromisoformat(meta["acted_at"])
        title_suffix = f"-{item['id'][:8]}"
        title = get_validate_filename(
            f"{item['meta']['action']}-{item['target']['title']}{title_suffix}",
            reserved_suffix=title_suffix,
        )
        target_dir = self._base_results_dir.joinpath(
            people,
            self.output_name,
            acted_at.strftime("%Y/%m/%d"),
            title,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = target_dir.joinpath(f"{title}.{self.save_type}")
        try:
            text_archive = await self.extract_text_archive(
                page,
                target,
                target_type,
                url,
            )
            text_archive_files = await self.save_text_archive(
                target_dir,
                title,
                text_archive,
            )
        except (OSError, PlaywrightError) as error:
            text_archive_files = {}
            self.logger.warning(f"Failed to save text archive: {error}")
        page_scroll_height = await page.evaluate(get_page_scrollHeight)
        if 0 < self.screenshot_max_page_scroll_height < page_scroll_height:
            page_scroll_width = await page.evaluate(get_page_scrollWidth)
            clip = {
                "x": 0,
                "y": 0,
                "width": page_scroll_width,
                "height": self.screenshot_max_page_scroll_height,
            }
            self.logger.warning(
                f"Page's scrollHeight({page_scroll_height}) is greater than `screenshot_max_page_scroll_height`({self.screenshot_max_page_scroll_height})."
            )
        else:
            clip = None
        self.logger.info(f"Saving screenshot to {screenshot_path}.")
        await page.screenshot(
            path=screenshot_path, type=self.save_type, full_page=True, clip=clip
        )
        info = {
            "title": target["title"],
            "url": url,
            "author": target["author"],
            "shot_at": now,
            "text_archive": text_archive_files,
        }
        info_path = target_dir.joinpath("info.json")
        async with aiofiles.open(info_path, "w", encoding="utf-8") as fp:
            await fp.write(
                json.dumps(info, ensure_ascii=False, indent=2, cls=JSONEncoder)
            )
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.5)
        await page.keyboard.press("PageDown")
        results_root = pathlib.Path(self._base_results_dir).expanduser().resolve()
        archive_path = target_dir.resolve().relative_to(results_root).as_posix()
        files = {
            "screenshot": screenshot_path.name,
            "info": info_path.name,
        }
        files.update(text_archive_files)
        return ArchiveResult(
            archive_path=archive_path,
            files=files,
        )

    async def store(
        self,
        playwright: Playwright,
        item_list: list[ActivityItem],
        headless: bool = True,
        **context_extra: object,
    ) -> list[ArchiveResult]:
        """依次归档一组任务并返回每条任务的产物信息。

        Args:
            playwright: 当前 Playwright 驱动。
            item_list: 待归档的动态数据。
            headless: 是否使用无头浏览器。
            **context_extra: 创建浏览器上下文时使用的额外参数。
        """
        results: list[ArchiveResult] = []
        async with self.get_context(
            playwright,
            browser_headless=headless,
            **context_extra,
        ) as context:
            empty_page = await self.new_page(context)
            self.logger.info(f"Will fetch {len(item_list)} items")
            for item in item_list:
                # 每个对象都新开一个标签页
                page = await self.new_page(context)
                try:
                    result = await self.store_one(item, page=page)
                    if result is not None:
                        results.append(result)
                finally:
                    await page.close()
                await asyncio.sleep(1)
            self.logger.info("Fetch done")
            await empty_page.close()
        return results

    async def run_queue(
        self,
        wakeup_event: asyncio.Event,
        headless: bool = True,
        idle_timeout: int = 30,
        **context_extra,
    ) -> None:
        """
        持续从 SQLite 领取并执行归档任务。

        Args:
            wakeup_event: 生产者提交任务后用于唤醒消费者的进程内事件。
            headless: 是否使用无头浏览器。
            idle_timeout: 队列空闲时兜底扫描间隔。
        """
        self.logger.info("archiver queue loop started.")
        while True:
            if await self.need_pause():
                await self.set_status(WorkStatus.WAITING)
                await asyncio.sleep(1)
                continue

            wakeup_event.clear()
            claimed_any = False
            while task := await self.pop_task():
                claimed_any = True
                await self.set_status(WorkStatus.RUNNING)
                self.logger.info(f"Claim archive task: {task.task_name}")
                try:
                    await self.configurator.load_to_worker(include_global=False)
                    async with Stealth().use_async(async_playwright()) as playwright:
                        if self.browser_semaphore is None:
                            results = await self.store(
                                playwright,
                                [task.payload],
                                headless,
                                **context_extra,
                            )
                        else:
                            async with self.browser_semaphore:
                                results = await self.store(
                                    playwright,
                                    [task.payload],
                                    headless,
                                    **context_extra,
                                )
                    if not results:
                        raise RuntimeError("归档任务未生成任何产物")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.logger.exception(error)
                    await self.sqlite_store.mark_archive_task_failed(
                        task.task_name,
                        str(error),
                    )
                else:
                    await self.sqlite_store.mark_archive_task_done(
                        task.task_name,
                        results[0],
                    )

            await self.set_status(WorkStatus.WAITING)
            if not claimed_any:
                try:
                    await asyncio.wait_for(wakeup_event.wait(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    pass
