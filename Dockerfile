FROM python:3.12-slim

# git for mirroring, git-lfs for large objects, rsync for the protection copy,
# ca-certificates for HTTPS to github.com and api.telegram.org
RUN apt-get update && apt-get install -y --no-install-recommends \
        git git-lfs rsync ca-certificates \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The lock pins the indirect dependencies too, so two builds of one commit
# get the same versions.
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY app ./app

# Non-root. The mounted NAS shares must be writable by this uid (see README).
RUN useradd -m -u 1000 ripper && mkdir -p /data && chown ripper:ripper /data
USER ripper

EXPOSE 8019

# No curl in the slim image, so the check uses the Python that is already here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8019/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8019"]
