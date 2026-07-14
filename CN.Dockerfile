FROM python:3.12-bookworm

ARG TZ=Asia/Shanghai
ENV TZ=${TZ}

RUN echo deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware > /etc/apt/sources.list && \
    echo deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware >> /etc/apt/sources.list && \
    echo deb https://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-backports main contrib non-free non-free-firmware >> /etc/apt/sources.list && \
    echo deb https://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware >> /etc/apt/sources.list

RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo ${TZ} > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip install --no-cache-dir uv==0.11.7

ENV UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
ENV PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

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

# 4. Compose 中的 worker 均为无头模式，只从国内镜像安装 Headless Shell
# 与系统依赖分层，浏览器下载失败重试时可以复用上一层缓存
RUN playwright install --only-shell chromium && \
    chmod -R 777 $PLAYWRIGHT_BROWSERS_PATH

COPY ./ /opt/zhi_archive
WORKDIR /opt/zhi_archive

# 5. 确保项目目录权限（可选，但在某些环境下对写文件很有必要）
RUN chmod -R 777 /opt/zhi_archive
