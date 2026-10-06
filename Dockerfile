FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
WORKDIR /srv/app

ENV DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8090 \
    REFRESH_MINUTES=20 \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=5).status == 200 else 1)"

CMD ["python", "-m", "waitress", "--host=0.0.0.0", "--port=8090", "--threads=8", "--call", "server:create_app"]
