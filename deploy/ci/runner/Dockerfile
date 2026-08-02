FROM python:3.12-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        > /etc/apt/apt.conf.d/80-myclaw-ci-retries \
    && sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        openssh-client \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-buildx-plugin \
        docker-ce-cli \
        docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /opt/myclaw-deps/backend/requirements.txt
COPY test/requirements.txt /opt/myclaw-deps/test/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/myclaw-ci \
    && /opt/myclaw-ci/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/myclaw-ci/bin/pip install -r /opt/myclaw-deps/test/requirements.txt

RUN PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000 \
    /opt/myclaw-ci/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

ENV MYCLAW_TEST_PYTEST=/opt/myclaw-ci/bin/pytest
ENV PATH=/opt/myclaw-ci/bin:$PATH

WORKDIR /workspace
CMD ["sleep", "infinity"]
