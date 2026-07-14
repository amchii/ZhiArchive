# Changelog

## Unreleased

### 破坏性变更

- 移除 Redis 和独立 worker 部署，改为单个 FastAPI 进程通过 lifespan 启动 monitor、archiver 和按需二维码登录任务。
- 运行时配置、暂停状态、抓取检查点、登录任务和归档任务队列改为保存到本地 SQLite，默认路径为 `var/zhi_archive.sqlite3`。
- 移除可编辑的 `global:state_path` 和登录任务中的临时 state 路径；已有可访问的旧 state 会在 SQLite schema v2 升级时迁移到托管文件。
- 本版本不提供 Redis 到 SQLite 的自动迁移工具；已有 Redis 数据需要手动迁移或以全新部署方式使用。

### 改进

- 知乎登录态改为应用托管的固定 state 文件；控制台支持上传 Playwright state 或浏览器 Cookies JSON，不再要求用户填写运行环境绝对路径。
- Worker 回写 storage state 时增加修订检查，避免运行中的旧浏览器上下文覆盖用户刚上传的新登录态。

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
