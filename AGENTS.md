# AGENTS.md

本文件面向在本仓库中工作的自动化代理和协作者。请优先遵循本文约定；如果与用户的明确指令冲突，以用户指令为准。

## 项目概览

ZhiArchive 是一个用于监测知乎用户动态并将相关内容保存到本地的 Python 项目，核心依赖包括 Playwright、FastAPI、SQLite 和 Pydantic Settings。

主要模块：

- `archive/core/monitor.py`：监测知乎用户动态，生成动态快照，并把待归档 payload 写入 SQLite。
- `archive/core/archiver.py`：打开目标回答或文章页面并保存截图和元信息。
- `archive/core/login.py`：处理知乎登录并获取运行所需认证状态。
- `archive/api/`：FastAPI 接口、页面渲染和鉴权逻辑。
- `archive/config.py`：运行配置入口，支持环境变量、`.env` 和 `.apienv`。
- `archive/storage.py`：SQLite schema、运行配置、抓取进度和任务队列。
- `archive/services.py`：FastAPI lifespan 中的后台服务容器。
- `run_api.sh`：单进程 API 入口；Monitor、archiver 和二维码登录任务由
  FastAPI lifespan 统一启动。
- `docker-compose.yaml`：容器化部署配置。

## 常用命令

创建或更新环境：

```sh
uv venv
uv pip install -r requirements.txt
uv pip install --group dev
. .venv/bin/activate
playwright install chromium
```

项目不提交 `uv.lock`；更新运行依赖导出文件时使用：

```sh
uv pip compile pyproject.toml -o requirements.txt
```

运行 API：

```sh
bash run_api.sh
```

Monitor、archiver 和二维码登录任务由 FastAPI lifespan 启动，不再单独运行 worker。

Docker 启动：

```sh
docker compose up -d
```

代码检查：

```sh
ruff check archive
```

测试：

```sh
pytest
```

## 开发约定

- 使用 Python 3.10 或更高版本。
- 保持现有模块边界：监控逻辑放在 `archive/core/monitor.py`，归档逻辑放在 `archive/core/archiver.py`，API 路由和渲染逻辑放在 `archive/api/`。
- 配置项优先通过 `archive/config.py` 统一管理，不要在业务逻辑中硬编码可配置参数。
- 文件路径、标题和截图文件名相关处理优先复用 `archive/utils/` 中的工具。
- 项目使用 Ruff，行宽按 `pyproject.toml` 中的 `88` 处理。
- 函数参数和返回值必须添加类型标注。
- 每个函数下至少保留一段简短说明（简体中文），描述该函数的作用。
- 参数 Docstring 使用 Google 风格。
- 修改异步 Playwright 流程时，注意浏览器上下文、页面关闭和异常恢复，避免 worker 长时间运行后资源泄漏。
- 对 SQLite 队列、cookie state、截图目录结构的改动应保持向后兼容，除非用户明确要求破坏性迁移。
- 不要从 API 配置或状态查询路径直接修改正在运行的后台 worker 实例；配置读写应通过 SQLite store 或临时配置对象完成。
- 修改 monitor checkpoint 时，应避免覆盖正在运行的抓取结果；默认要求 monitor 已暂停且不处于 running 状态。

## 运行与数据注意事项

- 本项目会保存知乎登录状态、截图、日志和归档内容。不要提交真实 cookie、账号密码、`.env`、`.apienv`、`states/`、`logs/`、`result/` 或 `results/` 中的个人数据。
- API 暴露到公网时，应启用 `.apienv` 中的鉴权配置。
- Playwright/Chromium 截图可能消耗较多内存；修改截图策略或并发策略时要考虑低内存部署环境。
- 知乎页面结构可能变化。调整选择器时，应尽量集中配置或封装，避免在多个模块中复制同一选择器。

## 提交前检查

在提交或交付前，至少确认：

- 相关入口脚本可以导入，无明显语法错误。
- 涉及 API 的改动已检查 FastAPI 路由和模板渲染路径。
- 涉及 Playwright 的改动已考虑登录态缺失、页面加载失败、超时和重试。
- 没有新增真实账号、cookie、截图结果、日志或本地缓存文件。

## 给代理的工作方式

- 先阅读相关文件，再做最小必要修改。
- 不要无关重构，不要格式化整个仓库。
- 不要删除用户已有数据目录或运行产物，除非用户明确要求。
- 如果需要联网安装依赖、运行 Docker、访问真实知乎页面或启动长期后台进程，先说明原因并等待用户确认。
