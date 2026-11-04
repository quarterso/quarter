# Pinned by digest: a re-pushed tag cannot change what is built. Bump tag and digest together.
FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# uv is mounted for this step only, so it is not in the image (28 MB). No cache is kept, and
# the code is compiled now: the ledger user cannot write __pycache__ under /app at run time.
RUN --mount=from=ghcr.io/astral-sh/uv:0.5.11@sha256:0ac957607303916420297a4c9c213bb33fbd3c888f9cd7f4f7273596ebf42b85,source=/uv,target=/bin/uv \
    uv sync --frozen --no-dev --no-cache && python -m compileall -q src \
    && useradd --uid 10001 --no-create-home ledger && mkdir /data && chown ledger /data

USER ledger
ENV PATH=/app/.venv/bin:$PATH \
    QUARTER_DB=/data/ledger.db \
    HOST=0.0.0.0 \
    PORT=8780
VOLUME /data
EXPOSE 8780

# A bare socket: importing urllib cost 47 ms of CPU per check, this costs 12 ms.
HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -S -c "import os, socket; s = socket.create_connection(('127.0.0.1', int(os.environ['PORT'])), 3); s.sendall(b'GET /healthz HTTP/1.0\r\n\r\n'); assert s.recv(12).endswith(b' 200')"

CMD ["quarter-ledger", "serve"]
