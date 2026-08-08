FROM python:3.12-slim

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    chromium fonts-liberation fonts-noto-color-emoji fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --retries 10 --timeout 300 -r requirements.txt

RUN groupadd --gid 10001 myclaw \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin myclaw \
    && install -d -o 10001 -g 10001 /var/lib/myclaw

COPY --chown=10001:10001 . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

USER 10001:10001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
