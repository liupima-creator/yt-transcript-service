FROM python:3.11-slim

# ffmpeg 是 yt-dlp 处理某些字幕/音视频格式时需要的
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Railway/Render 会通过 PORT 环境变量告诉你监听哪个端口
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
