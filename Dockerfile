FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY sorter.py base_request.json ./

# Sign-in, progress and the sort log live here: mount a persistent volume at /data
VOLUME /data

CMD ["python", "sorter.py", "--loop"]
