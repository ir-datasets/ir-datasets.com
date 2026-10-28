FROM python:3.12-slim

# git: ir_datasets (v2 branch) is installed from GitHub, see pyproject.toml
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . gunicorn \
    && apt-get purge -y git && apt-get autoremove -y

RUN useradd --system --create-home --uid 10001 attest \
    && mkdir /data && chown attest /data
USER attest

ENV ATTEST_DB=/data/attest.db
VOLUME /data
EXPOSE 8000

# The store holds a single SQLite connection guarded by a lock: one worker process, several threads.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", \
     "--access-logfile", "-", "ir_datasets_attest_server.wsgi:application"]
