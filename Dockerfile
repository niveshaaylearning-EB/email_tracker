FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py base_request.json ./
COPY web ./web

# Sign-in, sorter progress, the permanent email record and dashboard files live here:
# mount a persistent volume at /data
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health')"

# Sorter + daily tracker + password-protected dashboard
CMD ["python", "main.py"]
