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
import requests
from fastapi import FastAPI, HTTPException, Query
import yt_dlp

app = FastAPI(title="YouTube Transcript Service")

# 简单的密钥保护，防止别人白嫖你的服务器资源和流量。
# 部署时通过环境变量 API_KEY 设置，不要写死在代码里。
API_KEY = os.environ.get("API_KEY", "")

# 没有字幕的视频，会用 Groq 的 Whisper 语音转文字 API 兜底。
# 没配这个变量的话，没字幕的视频就还是只能返回"没有字幕"。
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


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
        text_lines = [
            ln for ln in block.splitlines()
            if "-->" not in ln and ln.strip() and not ln.strip().startswith("WEBVTT")
        ]
        text = " ".join(text_lines).strip()
        text = re.sub(r"<[^>]+>", "", text)
        if not text or text == last_text:
            continue
        last_text = text
        hh, mm, ss = timestamp.split(":")
        short_ts = f"{mm}:{ss}"
        lines_out.append(f"[{short_ts}] {text}")

    return "\n".join(lines_out)


def _transcribe_with_groq(audio_path: str) -> str:
    """把音频文件发给 Groq 的 Whisper 接口，返回带时间戳的文字稿。"""
    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            data={
                "model": GROQ_WHISPER_MODEL,
                "response_format": "verbose_json",
            },
            files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
            timeout=300,
        )

    if resp.status_code != 200:
        raise RuntimeError(f"Groq 转录接口报错 ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    segments = data.get("segments")
    if not segments:
        return data.get("text", "").strip()

    lines_out = []
    for seg in segments:
        start = seg.get("start", 0)
        mm = int(start // 60)
        ss = int(start % 60)
        text = seg.get("text", "").strip()
        if text:
            lines_out.append(f"[{mm:02d}:{ss:02d}] {text}")
    return "\n".join(lines_out)


def _download_audio(url: str, tmp_dir: str, player_clients):
    """下载视频音频（转成小体积的低码率 mp3），用于没有字幕时的语音转录兜底。"""
    outtmpl = os.path.join(tmp_dir, "audio_%(id)s.%(ext)s")
    base_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "64",
        }],
        "postprocessor_args": {
            "ffmpeg": ["-ar", "16000", "-ac", "1"],
        },
    }

    info = None
    last_error = None
    for client in player_clients:
        ydl_opts = dict(base_opts)
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception as e:
            last_error = e
            continue

    if info is None:
        raise RuntimeError(f"音频下载失败（已尝试多种客户端伪装）: {last_error}")

    mp3_files = glob.glob(os.path.join(tmp_dir, "*.mp3"))
    if not mp3_files:
        raise RuntimeError("音频下载完成但没找到 mp3 文件")

    return info, mp3_files[0]


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

        base_opts = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs,
            "subtitlesformat": "vtt",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
        }

        player_clients_to_try = ["android", "tv", "web_creator", "ios"]

        info = None
        last_error = None
        for client in player_clients_to_try:
            ydl_opts = dict(base_opts)
            ydl_opts["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                last_error = e
                continue

        if info is None:
            raise HTTPException(
                status_code=502,
                detail=f"yt-dlp 处理失败（已尝试多种客户端伪装均失败）: {last_error}",
            )

        vtt_files = glob.glob(os.path.join(tmp_dir, "*.vtt"))

        if not vtt_files:
            if not GROQ_API_KEY:
                return {
                    "title": info.get("title"),
                    "duration_seconds": info.get("duration"),
                    "source": "none",
                    "transcript": "",
                    "note": "这个视频没有可用字幕（人工或自动都没有），且未配置 Groq 语音转录（GROQ_API_KEY）。",
                }
            try:
                audio_info, mp3_path = _download_audio(url, tmp_dir, player_clients_to_try)
                transcript = _transcribe_with_groq(mp3_path)
                return {
                    "title": info.get("title"),
                    "duration_seconds": info.get("duration"),
                    "source": "groq_whisper",
                    "transcript": transcript,
                    "note": "该视频没有字幕，此文字稿由 Groq Whisper 语音识别自动生成，可能有转录误差。",
                }
            except Exception as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"没有字幕，尝试语音转录也失败了: {e}",
                )

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

        return {
            "title": info.get("title"),
            "duration_seconds": info.get("duration"),
            "source_file": os.path.basename(chosen),
            "transcript": transcript,
        }


@app.get("/health")
def health():
    return {"status": "ok"}
