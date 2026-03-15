FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY dashboard_template.html .
COPY hep_scraper.py .
COPY ha_sender.py .
COPY generate_dashboard.py .
COPY sync_all.py .
COPY sync_loop.sh .
COPY sma_scraper.py .
COPY sma_history_import.py .
RUN mkdir -p /data
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD wget -qO- http://localhost:5000/health || exit 1
CMD ["python", "app.py"]
