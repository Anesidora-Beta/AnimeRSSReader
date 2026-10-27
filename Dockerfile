# 动漫资源 · RSS 阅读器
# 纯标准库镜像，约 60MB；数据用 volume 持久化
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="anime-rss-reader" \
      org.opencontainers.image.description="多站动漫资源聚合 + RSS 阅读器" \
      org.opencontainers.image.version="1.5.0"

WORKDIR /app

# 只复制运行必需的文件
COPY app.py feeds.py store.py sources.py textutil.py schedule.py settings.py ./
COPY anime_rss/ ./anime_rss/
COPY static/ ./static/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8899 \
    ANIME_RSS_DATA=/data \
    ANIME_RSS_NO_BROWSER=1 \
    ANIME_RSS_QUIET=1

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8899

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8899/healthz', timeout=4).status==200 else 1)"

CMD ["python", "app.py"]
