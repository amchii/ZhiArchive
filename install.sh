#!/usr/bin/env bash

set -Eeuo pipefail

REPOSITORY="${ZHIARCHIVE_REPOSITORY:-}"
INSTALL_DIR="${ZHIARCHIVE_INSTALL_DIR:-}"
GIT_REF="${ZHIARCHIVE_REF:-}"
USE_GITEE=false
DOCKERFILE="Dockerfile"
START_SERVICES=true
IMAGE_NAME="zhi-archive:latest"

# 输出普通安装进度。
log() {
    printf '[ZhiArchive] %s\n' "$*"
}

# 输出错误信息并终止安装。
fail() {
    printf '[ZhiArchive] 错误：%s\n' "$*" >&2
    exit 1
}

# 显示命令行帮助。
show_help() {
    cat <<'EOF'
用法：
  bash install.sh [选项]
  curl -fsSL https://raw.githubusercontent.com/amchii/ZhiArchive/main/install.sh | bash
  curl -fsSL https://raw.giteeusercontent.com/amchii/ZhiArchive/raw/main/install.sh | bash -s -- --gitee --cn

选项：
  --dir <目录>       clone 目标目录，默认为当前目录下的 ZhiArchive
  --repo <地址>      Git 仓库地址
  --ref <分支或标签> clone 指定分支或标签
  --gitee            从 Gitee clone，--repo 的自定义地址优先
  --cn               使用 CN.Dockerfile 构建国内源镜像
  --no-start         只 clone、初始化并构建镜像，不启动服务
  -h, --help         显示帮助

也可使用环境变量 ZHIARCHIVE_INSTALL_DIR、ZHIARCHIVE_REPOSITORY 和
ZHIARCHIVE_REF 设置对应参数。
EOF
}

# 检查必需命令是否存在。
require_command() {
    local command_name="$1"
    local install_hint="$2"

    if ! command -v "$command_name" >/dev/null 2>&1; then
        fail "未找到 ${command_name}。${install_hint}"
    fi
}

# 判断目录是否为可部署的 ZhiArchive 仓库。
is_project_dir() {
    local directory="$1"

    [[ -f "${directory}/archive/config.py" && \
        -f "${directory}/pyproject.toml" && \
        -f "${directory}/docker-compose.yaml" && \
        -f "${directory}/Dockerfile" ]]
}

# 生成供应用签名使用的随机密钥。
generate_secret_key() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return
    fi

    LC_ALL=C od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

# 创建运行目录和最小本地配置，保留用户已有内容。
initialize_project() {
    local project_dir="$1"
    local env_file="${project_dir}/.env"

    mkdir -p \
        "${project_dir}/logs" \
        "${project_dir}/results" \
        "${project_dir}/states"

    if [[ ! -e "$env_file" ]]; then
        umask 077
        printf 'secret_key=%s\n' "$(generate_secret_key)" >"$env_file"
        log "已生成 .env 和随机 secret_key"
    else
        log "检测到已有 .env，保持不变"
    fi
}

# 在远程执行时 clone 仓库，仓库内执行时复用当前工作区。
resolve_project_dir() {
    local script_path="${BASH_SOURCE[0]:-}"
    local script_dir=""
    local current_dir

    current_dir="$(pwd -P)"
    if [[ -n "$script_path" && -f "$script_path" ]]; then
        script_dir="$(cd "$(dirname "$script_path")" && pwd -P)"
        if is_project_dir "$script_dir"; then
            printf '%s\n' "$script_dir"
            return
        fi
    fi

    if is_project_dir "$current_dir"; then
        printf '%s\n' "$current_dir"
        return
    fi

    if [[ -z "$INSTALL_DIR" ]]; then
        INSTALL_DIR="${current_dir}/ZhiArchive"
    elif [[ "$INSTALL_DIR" != /* ]]; then
        INSTALL_DIR="${current_dir}/${INSTALL_DIR}"
    fi

    if is_project_dir "$INSTALL_DIR"; then
        printf '%s\n' "$(cd "$INSTALL_DIR" && pwd -P)"
        return
    fi

    if [[ -e "$INSTALL_DIR" ]]; then
        [[ -d "$INSTALL_DIR" ]] || fail "目标路径不是目录：${INSTALL_DIR}"
        if [[ -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
            fail "目标目录已存在且不是 ZhiArchive 仓库：${INSTALL_DIR}"
        fi
    fi

    log "正在 clone ${REPOSITORY} 到 ${INSTALL_DIR}" >&2
    local -a clone_args=(clone --depth 1)
    if [[ -n "$GIT_REF" ]]; then
        clone_args+=(--branch "$GIT_REF")
    fi
    clone_args+=("$REPOSITORY" "$INSTALL_DIR")
    git "${clone_args[@]}" >&2 || fail "clone 失败，请检查仓库地址和网络"

    printf '%s\n' "$(cd "$INSTALL_DIR" && pwd -P)"
}

# 解析安装参数。
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir)
            [[ $# -ge 2 ]] || fail "--dir 缺少目录参数"
            INSTALL_DIR="$2"
            shift 2
            ;;
        --repo)
            [[ $# -ge 2 ]] || fail "--repo 缺少仓库地址"
            REPOSITORY="$2"
            shift 2
            ;;
        --ref)
            [[ $# -ge 2 ]] || fail "--ref 缺少分支或标签"
            GIT_REF="$2"
            shift 2
            ;;
        --gitee)
            USE_GITEE=true
            shift
            ;;
        --cn)
            DOCKERFILE="CN.Dockerfile"
            shift
            ;;
        --no-start)
            START_SERVICES=false
            shift
            ;;
        -h | --help)
            show_help
            exit 0
            ;;
        *)
            fail "未知参数：$1（使用 --help 查看帮助）"
            ;;
    esac
done

if [[ -z "$REPOSITORY" ]]; then
    if [[ "$USE_GITEE" == true ]]; then
        REPOSITORY="https://gitee.com/amchii/ZhiArchive.git"
    else
        REPOSITORY="https://github.com/amchii/ZhiArchive.git"
    fi
fi

if [[ "$USE_GITEE" == true && "$DOCKERFILE" == "Dockerfile" ]]; then
    log "提示：当前仅从 Gitee clone，构建仍使用默认源；国内构建请同时传入 --cn"
fi

require_command git "请先安装 Git。"
require_command docker "请先安装并启动 Docker。"

if ! docker compose version >/dev/null 2>&1; then
    fail "当前 Docker 未提供 Compose v2，请安装 Docker Compose 插件或 Docker Desktop。"
fi
if ! docker info >/dev/null 2>&1; then
    fail "无法连接 Docker daemon，请确认 Docker 已启动且当前用户有访问权限。"
fi

PROJECT_DIR="$(resolve_project_dir)"
is_project_dir "$PROJECT_DIR" || fail "项目文件不完整：${PROJECT_DIR}"
[[ -f "${PROJECT_DIR}/${DOCKERFILE}" ]] || fail "未找到 ${DOCKERFILE}"

initialize_project "$PROJECT_DIR"

log "正在使用 ${DOCKERFILE} 构建 ${IMAGE_NAME}"
docker build \
    --tag "$IMAGE_NAME" \
    --file "${PROJECT_DIR}/${DOCKERFILE}" \
    "$PROJECT_DIR"

if [[ "$START_SERVICES" == true ]]; then
    log "正在启动 API、Redis 和 workers"
    docker compose \
        --project-directory "$PROJECT_DIR" \
        --file "${PROJECT_DIR}/docker-compose.yaml" \
        up -d
    docker compose \
        --project-directory "$PROJECT_DIR" \
        --file "${PROJECT_DIR}/docker-compose.yaml" \
        ps

    log "安装完成：${PROJECT_DIR}"
    log "控制台：http://127.0.0.1:9090/zhi/core/config"
else
    log "初始化和镜像构建完成：${PROJECT_DIR}"
    log "稍后可在该目录运行：docker compose up -d"
fi
