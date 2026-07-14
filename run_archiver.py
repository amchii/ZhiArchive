def main() -> None:
    """提示用户使用单进程 API 入口启动 archiver。"""
    raise SystemExit(
        "archiver 已合并到 FastAPI lifespan，请使用 bash run_api.sh 启动。"
    )


if __name__ == "__main__":
    main()
