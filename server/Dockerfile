FROM python:3.11-slim

# Switch apt to a faster mirror in CN regions; harmless elsewhere because
# debian.trixie doesn't ship fast everywhere on bare Docker Hub.
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
     || sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list

WORKDIR /app

# System deps for asyncpg / geoalchemy
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq5 \
        && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
 && pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn \
 && pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

COPY alembic ./alembic
COPY alembic.ini ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

EXPOSE 8000

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "rid_server.app:app", "--host", "0.0.0.0", "--port", "8000"]
