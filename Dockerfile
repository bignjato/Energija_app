FROM python:3.12-slim

# Oba kontejnera (web + sync) rade u hrvatskom lokalnom vremenu: datetime.now()
# i `date` u sync_loop.sh su zidni sat.
ENV TZ=Europe/Zagreb

# tzdata: python:3.12-slim danas ima /usr/share/zoneinfo; instalira se samo ako
# ga bazni image jednom prestane isporučivati (bez njega TZ tiho pada na UTC).
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 \
        curl \
        rsync \
        openssh-client \
        rclone \
    && { [ -f /usr/share/zoneinfo/Europe/Zagreb ] \
         || apt-get install -y --no-install-recommends tzdata; } \
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

# gthread: 2 workera x 4 threada — spor upstream (HA, vrijeme, PDF import) više
# ne blokira /health i login. --timeout 120 jer ti zahtjevi legitimno traju >30 s.
# --forwarded-allow-ips "*" je sigurno SAMO zato što je port objavljen isključivo
# na loopbacku (docker-compose: 127.0.0.1/[::1]) pa do gunicorna dolazi jedino
# host nginx; uz javno izložen port klijent bi mogao lažirati X-Forwarded-For.
CMD ["gunicorn", "-k", "gthread", "-w", "2", "--threads", "4", "--timeout", "120", \
     "-b", "0.0.0.0:5000", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--forwarded-allow-ips", "*", "app:app"]
