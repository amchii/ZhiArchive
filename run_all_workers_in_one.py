def main() -> None:
    """提示用户 worker 已合并到 API 单进程。"""
    raise SystemExit(
        "所有 worker 已合并到 FastAPI lifespan，请使用 bash run_api.sh 启动。"
    )


if __name__ == "__main__":
    main()
