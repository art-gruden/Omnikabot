FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data TZ=Europe/Berlin
WORKDIR /app

# tzdata — щоб TZ справді діяв: від нього залежить, яке фото вважається «сьогоднішнім»
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY textcard textcard
COPY bot bot
COPY fonts fonts
COPY templates templates
COPY prompts prompts

RUN useradd --system --uid 10001 --no-create-home cardbot \
 && mkdir -p /data/photos && chown -R 10001:10001 /data
USER 10001
VOLUME /data

CMD ["python", "-m", "bot.main"]
