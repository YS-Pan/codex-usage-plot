"""Locate an existing encoder; business calls never download executables."""

from __future__ import annotations

import os
import shutil


def ffmpeg_executable() -> str:
    configured = os.environ.get("CODEX_USAGE_FFMPEG_EXE") or os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured:
        return configured
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError as exc:
        if exc.name != "imageio_ffmpeg":
            raise
        raise RuntimeError(
            "找不到 FFmpeg。请安装 FFmpeg 并加入 PATH，或设置 CODEX_USAGE_FFMPEG_EXE 指向已有程序；"
            "使用 report --no-animations 可仅更新图片。工具不会自动下载编码器。"
        ) from None
    return imageio_ffmpeg.get_ffmpeg_exe()
