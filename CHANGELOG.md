# Changelog

## Unreleased

## 26.7.1 - 2026-07-24

本版本完成单进程 SQLite 架构落地，新增结果浏览器和带独立 Token 的 MCP 接入，并清理旧的多 worker 启动方式。

### 破坏性变更

- 移除 Redis 和独立 worker 部署，改为单个 FastAPI 进程通过 lifespan 启动 monitor、archiver 和按需二维码登录任务。
- 运行时配置、暂停状态、抓取检查点、登录任务和归档任务队列改为保存到本地 SQLite，默认路径为 `var/zhi_archive.sqlite3`。
- 移除可编辑的 `global:state_path` 和登录任务中的临时 state 路径；已有可访问的旧 state 会在 SQLite schema v2 升级时迁移到托管文件。
- 本版本不提供 Redis 到 SQLite 的自动迁移工具；已有 Redis 数据需要手动迁移或以全新部署方式使用。
- 删除 `run_monitor.py`、`run_archiver.py`、`run_login_worker.py`、`run_all_workers_in_one.py`、`docker-compose2.yaml`、`pull_and_build.sh` 和旧 seccomp 配置；统一使用 `run_api.sh` 或 Docker Compose 启动主服务。

### 新增

- 新增只读结果浏览器，可在控制台查看 `results/<people>/activities` 和 `results/<people>/archives` 中的图片、JSON、HTML、Markdown 和纯文本归档。
- 新增 Streamable HTTP MCP 服务，支持 AI Agent 读取知乎内容、提交归档任务、查询任务状态和发起二维码登录。
- 控制台新增 MCP 配置区，可启停 MCP、配置 Reader 超时和正文长度上限，并生成或轮换独立 Bearer Token。
- 新增 `ReaderWorker`，复用知乎登录态读取回答、文章和想法正文，供 MCP 工具按需调用。
- 新增 Bash 和 PowerShell 一键 Docker 安装脚本，支持 GitHub/Gitee 源和国内镜像安装方式。
- 新增单进程 SQLite 架构说明文档。

### 改进

- 知乎登录态改为应用托管的固定 state 文件；控制台支持上传 Playwright state 或浏览器 Cookies JSON，不再要求用户填写运行环境绝对路径。
- Worker 回写 storage state 时增加修订检查，避免运行中的旧浏览器上下文覆盖用户刚上传的新登录态。
- Dockerfile、CN.Dockerfile、`docker-compose.yaml` 和 README 适配单进程服务和持久化目录布局。
- 控制台增加归档结果入口，并完善 worker 状态、MCP 状态和运行配置展示。
- Monitor 新增动态内容加载超时配置项，降低知乎页面动态加载异常时的阻塞风险。
- README 将公网暴露、鉴权、MCP Token、Docker 持久化和登录态保护说明整合到独立“安全”小节。

### 修复

- 拒绝保存 `NaN`、`Infinity` 等非有限 cookie expiry 值，避免无效浏览器 Cookie 导致登录态写入或加载异常。
- 二维码登录超时后不再用未登录浏览器状态覆盖已有有效登录态。
- MCP 日志处理避免把业务 INFO 日志写入 FastMCP root handler。

### 维护

- 新增和更新 auth state、SQLite store、MCP、Reader、结果浏览器、登录、配置和归档相关测试。

## 26.7.0 - 2026-07-09

本版本整理了自 `37889acc` 以来的功能改动和兼容性修复。

### 新增

- 使用 `uv` 管理项目依赖，新增 `dependency-groups.dev` 用于开发依赖。
- 新增手动归档能力，可在控制台提交知乎回答或专栏文章链接，由 archiver 主动保存。
- 新增回答和文章的 HTML、Markdown 文本归档，和截图、`info.json` 保存在同一目录。
- 新增赞同或发布“想法”的监测与截图保存能力。
- 新增全局目标用户配置，monitor 和 archiver 共享同一个知乎用户 ID。
- 控制台新增表单化配置编辑、JSON 查看、现代化提示和运行状态控制。
- 登录页新增手动获取二维码入口，避免首次进入页面就请求二维码。

### 改进

- README、Dockerfile、CN.Dockerfile 和依赖安装流程适配 `uv`。
- 国内镜像场景下不提交 `uv.lock`，继续使用 `requirements.txt` 作为运行依赖导出文件。
- 配置页作为 API 控制台首页，优化了配置和运行状态布局。
- Archiver 截图前会隐藏知乎页面中的固定操作栏，降低长截图中标题栏或“赞同/评论”栏漂移遮挡正文的概率。
- 归档文件名截断时保留任务短 ID，避免长标题导致文件名尾部关键信息丢失。
- 手动归档任务会补全目标页面标题和作者信息。
- 文本归档失败时不影响截图和 `info.json` 保存。

### 修复

- 修复依赖升级后 FastAPI/Jinja2 `TemplateResponse` 参数顺序不兼容的问题。
- 修复回答和文章长截图中知乎浮动操作栏漂移到正文中间的问题。
- 修复 Archiver 截图前只隐藏当前浮动元素，无法覆盖后续重建浮动栏的问题。
- 修复想法正文较长时需要展开“阅读全文”才能完整截图的问题。
- 修复文本归档中图片没有 `figcaption` 时 Markdown 渲染报错的问题。
- 修复文章文本抽取可能 fallback 到过宽 `.Post-content`，混入推荐区内容的问题。

### 维护

- License 调整为 MIT。
- 新增并完善 archiver、monitor、工具函数和 API 的单元测试。
- 更新开发检查命令，覆盖 `archive` 和 `tests`。
