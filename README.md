# ZhiArchive

监测知乎用户动态，并将相关回答、文章和想法保存到本地。

当前版本：`26.7.0`

ZhiArchive 基于 Playwright、FastAPI 和 Redis 工作，适合长期运行在本机或服务器上，用于保存指定知乎用户的公开动态和动态关联内容。

## 功能

- 监测指定知乎用户的动态页。
- 保存动态卡片截图和动态 JSON 快照。
- 对动态中的回答和文章触发归档，保存长截图、元信息、HTML 和 Markdown。
- 对赞同或发布的想法保存动态截图。
- 支持在控制台手动提交回答或文章链接，主动触发归档。
- 提供 Web 控制台管理登录状态、Cookie 路径、目标用户、运行状态和 worker 配置。
- 支持 Docker 部署，也支持本地 `uv` 环境运行。

## 输出结构

某个用户的归档结果大致如下：

```text
results/<people>/
├── activities
│   ├── 2026
│   │   └── 07
│   │       └── 09
│   │           └── 赞同-某条动态-12345678.jpeg
│   └── 20260709120000.json
├── archives
│   └── 2026
│       └── 07
│           └── 09
│               └── 赞同-某篇回答-12345678
│                   ├── info.json
│                   ├── 赞同-某篇回答-12345678.jpeg
│                   ├── 赞同-某篇回答-12345678.html
│                   └── 赞同-某篇回答-12345678.md
└── tasks
    └── manual-20260709120000-12345678.json
```

`info.json` 示例：

```json
{
  "title": "某篇回答标题",
  "url": "https://www.zhihu.com/question/1/answer/2",
  "author": "author-id",
  "shot_at": "2026-07-09T12:00:00.000000",
  "text_archive": {
    "html": "赞同-某篇回答-12345678.html",
    "markdown": "赞同-某篇回答-12345678.md"
  }
}
```

说明：

- `activities` 保存动态页卡片截图和本次监测到的动态 JSON。
- `archives` 保存回答或文章归档。
- `tasks` 保存待 archiver 消费的任务文件。
- HTML 是更接近知乎正文排版的文本归档，Markdown 便于搜索、阅读和二次处理。

动态卡片截图示例：

<p>
  <img src="./docs/static/dynamic_screenshot.png" alt="动态截图" width="720">
</p>

回答/文章截图示例：

<p>
  <img src="./docs/static/content_screenshot.png" alt="内容截图" width="720">
</p>

## 模块

- `login worker`：获取知乎登录二维码，并保存 Playwright storage state。
- `monitor`：监测目标用户动态，保存动态截图，并将回答/文章任务推给 archiver。
- `archiver`：打开回答或文章页面，保存截图、HTML、Markdown 和元信息。
- `api`：提供 Web 控制台和配置接口。
- `redis`：保存 worker 状态、配置和归档任务队列。

## 本地运行

项目依赖由 `uv` 管理，不提交 `uv.lock`。首次运行或依赖变更后执行：

```sh
uv venv
uv pip install -r requirements.txt
uv pip install --group dev
. .venv/bin/activate
playwright install chromium
```

如需使用国内 PyPI 镜像，可在安装前设置：

```sh
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple/
```

运行 API：

```sh
bash run_api.sh
```

或分别运行 worker：

```sh
python run_login_worker.py
python run_monitor.py
python run_archiver.py
python run_all_workers_in_one.py
```

代码检查和测试：

```sh
ruff check archive tests
pytest
```

更新运行依赖导出文件：

```sh
uv pip compile pyproject.toml -o requirements.txt
```

## Docker

下载项目：

```sh
git clone https://github.com/amchii/ZhiArchive.git
cd ZhiArchive
```

构建国内源镜像：

```sh
docker build -t zhi-archive:latest -f CN.Dockerfile .
```

每个 worker 单独运行：

```sh
docker compose up -d
```

多个 worker 在一个容器中运行，并单独部署 Redis：

```sh
docker compose -f docker-compose2.yaml up -d
```

如果单独部署 Redis，可通过环境变量或 `.env` 配置：

```env
redis_host=172.17.0.1
redis_port=6379
redis_passwd=apassword
```

注意：容器内 Chromium 在 root 用户下无法以沙盒模式启动。公网部署时应限制容器权限和内存，并参考 Playwright Docker 安全建议。

## 初始化

默认 API 端口是 `9090`。以本机为例，打开：

```text
http://127.0.0.1:9090/zhi/core/config
```

首次使用建议按以下步骤操作：

1. 确认 `login worker` 已启动。
2. 在控制台点击“去登录知乎”，进入登录页。
3. 点击获取二维码按钮，使用知乎 App 扫码登录。
4. 登录成功后返回配置页。
5. 在“目标用户”中填写知乎用户 ID。
6. 确认 Cookie state 路径。
7. 配置 monitor 和 archiver 参数。
8. 在“运行状态”中切换 worker 状态。

如果已有可用的 Playwright storage state 文件，也可以直接在配置页设置 state 文件路径，不必重新扫码。

登录二维码页面示例：

<p>
  <img src="./docs/static/qrcode_login.jpg" alt="二维码登录" width="560">
</p>

配置控制台示例：

<p>
  <img src="./docs/static/config.jpg" alt="配置页" width="900">
</p>

## 配置

配置来源包括环境变量、`.env`、`.apienv` 和 Redis 中的运行时配置。常见配置项见 [archive/config.py](./archive/config.py)。

公网暴露 API 时，建议启用 `.apienv` 鉴权：

```env
enable_auth=true
username=
password=
```

控制台中的目标用户是全局配置，会同时影响 monitor 和 archiver。monitor 和 archiver 的可编辑配置只保存 worker 专属参数。

## 手动归档

控制台支持直接提交知乎回答或专栏文章链接，例如：

```text
https://www.zhihu.com/question/2058247449894970042/answer/2058308278786987158
https://zhuanlan.zhihu.com/p/2055288243885709032
```

提交后会写入现有任务队列，由 archiver 按普通归档流程处理。

## 文本归档

回答和文章归档时会同时保存：

- 截图：`jpeg` 或 `png`
- HTML：更接近知乎正文排版
- Markdown：便于检索和二次处理
- 元信息：`info.json`

HTML 和 Markdown 是截图之外的附加产物。文本归档失败不会中断截图保存。

## 已知问题

- Chromium 截图占用内存较高，低内存服务器可能出现浏览器崩溃。
- 超长回答或文章仍可能触发 Playwright 截图失败。
- 知乎页面结构变化可能导致选择器失效，需要随页面更新适配。
- HTML/Markdown 文本归档更适合正文保存，不保证完整还原所有知乎交互组件。

## 变更记录

详见 [CHANGELOG.md](./CHANGELOG.md)。

## 许可证

本项目使用 MIT License。
