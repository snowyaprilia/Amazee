FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY proxy.py .
ENV AMAZEE_PROXY_HOST=0.0.0.0 \
    AMAZEE_PROXY_PORT=8127 \
    AMAZEE_PROXY_DB=/data/keys.db
VOLUME ["/data"]
EXPOSE 8127
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8127/health',timeout=3).status==200 else 1)"
CMD ["python", "proxy.py"]
