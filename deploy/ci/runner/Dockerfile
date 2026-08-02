FROM python:3.12-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
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

RUN python -m venv /opt/myclaw-ci \
    && /opt/myclaw-ci/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/myclaw-ci/bin/pip install --no-cache-dir -r /opt/myclaw-deps/test/requirements.txt \
    && /opt/myclaw-ci/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

ENV MYCLAW_TEST_PYTEST=/opt/myclaw-ci/bin/pytest
ENV PATH=/opt/myclaw-ci/bin:$PATH

WORKDIR /workspace
CMD ["sleep", "infinity"]
