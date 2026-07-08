from archive.utils.common import get_validate_filename


def test_get_validate_filename_preserves_reserved_suffix() -> None:
    """验证长标题被截断后仍保留短 ID 和扩展名。"""
    suffix = "-12345678.jpeg"
    filename = get_validate_filename(
        f"赞同-{'很长的想法正文' * 30}{suffix}",
        reserved_suffix=suffix,
    )

    assert filename.endswith(suffix)
    assert len(filename.removesuffix(suffix)) == 50
    assert len(filename.encode("utf-8")) <= 255
