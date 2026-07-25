FROM python:3.12-slim

# git for mirroring, git-lfs for large objects, rsync for the protection copy,
# ca-certificates for HTTPS to github.com and api.telegram.org
RUN apt-get update && apt-get install -y --no-install-recommends \
        git git-lfs rsync ca-certificates \
    && git lfs install --system \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Non-root. The mounted NAS shares must be writable by this uid (see README).
RUN useradd -m -u 1000 ripper && mkdir -p /data && chown ripper:ripper /data
USER ripper

EXPOSE 8019

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8019"]
