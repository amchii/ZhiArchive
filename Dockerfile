FROM python:3.12-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

# 1. 创建一个普通用户 (uid 1000 是常见标准，方便文件权限映射)
RUN useradd -m -u 1000 pwuser

# 2. 关键配置：设置 Playwright 浏览器下载路径为公共目录
# 这样无论 Root 还是 pwuser 都能找到浏览器
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p $PLAYWRIGHT_BROWSERS_PATH

COPY ./requirements.txt /opt/zhi_archive/requirements.txt
RUN uv pip install --system -r /opt/zhi_archive/requirements.txt

# 3. 安装 Chromium 系统依赖
# 注意：这一步需以 Root 运行，因为 install-deps 会调用 apt-get
RUN playwright install-deps chromium && rm -rf /var/lib/apt/lists/*

# 4. Compose 中的 worker 均为无头模式，只安装体积更小的 Headless Shell
# 与系统依赖分层，浏览器下载失败重试时可以复用上一层缓存
RUN playwright install --only-shell chromium && \
    chmod -R 777 $PLAYWRIGHT_BROWSERS_PATH

COPY ./ /opt/zhi_archive
WORKDIR /opt/zhi_archive

# 5. 确保项目目录权限（可选，但在某些环境下对写文件很有必要）
RUN chmod -R 777 /opt/zhi_archive
