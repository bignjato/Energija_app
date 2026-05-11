FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data && chown -R app:app /app /data

USER app

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s \
  CMD curl -fsS http://localhost:5000/health || exit 1

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--forwarded-allow-ips", "*", "app:app"]
