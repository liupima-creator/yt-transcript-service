"""
YouTube 转录服务 —— 最小可用版本 (MVP)

功能：给一个 YouTube 链接，返回视频标题 + 带时间戳的字幕/转录文本。
不下载视频、不截帧，只拿字幕，所以又快又省资源。

接口：
  GET /transcript?url=<youtube链接>&key=<你设置的密钥>

部署到 Railway / Render 后，你会得到一个公网地址，例如：
  https://your-app.up.railway.app

以后在 Claude 里，你直接说：
  "帮我看看这个视频讲了什么: https://your-app.up.railway.app/transcript?url=https://youtu.be/xxxx&key=你的密钥"

Claude 会自己去读取这个接口返回的内容，然后帮你总结。
"""

import os
import re
import glob
import tempfile
from fastapi import FastAPI, HTTPException, Query
import yt_dlp

app = FastAPI(title="YouTube Transcript Service")

# 简单的密钥保护，防止别人白嫖你的服务器资源和流量。
# 部署时通过环境变量 API_KEY 设置，不要写死在代码里。
API_KEY = os.environ.get("API_KEY", "")


def _check_key(key: str):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="密钥不对，检查一下 key 参数")


def _vtt_to_text(vtt_path: str) -> str:
    """把 .vtt 字幕文件转成干净的、带时间戳的纯文本。"""
    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    # 按字幕块切分
    blocks = re.split(r"\n\n+", raw)
    lines_out = []
    last_text = None
    time_re = re.compile(r"(\d{2}:\d{2}:\d{2})\.\d{3}\s*-->")

    for block in blocks:
        m = time_re.search(block)
        if not m:
            continue
        timestamp = m.group(1)
        # 去掉时间戳行和 WEBVTT 头，剩下的当作文本
        text_lines = [
            ln for ln in block.splitlines()
            if "-->" not in ln and ln.strip() and not ln.strip().startswith("WEBVTT")
        ]
        text = " ".join(text_lines).strip()
        # 去掉 <c> 之类的样式标签
        text = re.sub(r"<[^>]+>", "", text)
        if not text or text == last_text:
            continue  # yt-dlp 自动字幕经常有重复行，去重一下
        last_text = text
        # 时间戳只保留到分:秒，够用了
        hh, mm, ss = timestamp.split(":")
        short_ts = f"{mm}:{ss}"
        lines_out.append(f"[{short_ts}] {text}")

    return "\n".join(lines_out)


@app.get("/transcript")
def get_transcript(
    url: str = Query(..., description="YouTube 视频链接"),
    key: str = Query("", description="访问密钥"),
    lang: str = Query("zh-Hans,zh-Hant,en", description="优先语言，逗号分隔"),
):
    _check_key(key)

    with tempfile.TemporaryDirectory() as tmp_dir:
        outtmpl = os.path.join(tmp_dir, "%(id)s.%(ext)s")
        langs = [s.strip() for s in lang.split(",") if s.strip()]

        ydl_opts = {
            "skip_download": True,       # 不下载视频本体
            "writesubtitles": True,      # 有人工字幕就要
            "writeautomaticsub": True,   # 没有就用自动生成的
            "subtitleslangs": langs,
            "subtitlesformat": "vtt",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"yt-dlp 处理失败: {e}")

        vtt_files = glob.glob(os.path.join(tmp_dir, "*.vtt"))
        if not vtt_files:
            return {
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
                "source": "none",
                "transcript": "",
                "note": "这个视频没有可用字幕（人工或自动都没有）。",
            }

        # 按优先语言顺序找一个可用的字幕文件
        chosen = None
        for l in langs:
            for f in vtt_files:
                if f".{l}." in f or f.endswith(f".{l}.vtt"):
                    chosen = f
                    break
            if chosen:
                break
        if not chosen:
            chosen = vtt_files[0]

        transcript = _vtt_to_text(chosen)
        is_auto = "auto" in os.path.basename(chosen) or True  # yt-dlp 命名不总是可靠，保守标记

        return {
            "title": info.get("title"),
            "duration_seconds": info.get("duration"),
            "source_file": os.path.basename(chosen),
            "transcript": transcript,
        }


@app.get("/health")
def health():
    return {"status": "ok"}
