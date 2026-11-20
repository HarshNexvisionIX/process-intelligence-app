FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.lock.txt ./requirements.lock.txt
RUN python -m pip install --upgrade pip && pip install -r requirements.lock.txt

COPY app/ ./app/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY .streamlit/ ./.streamlit/
COPY docker/entrypoint.py ./docker/entrypoint.py

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser
EXPOSE 8501
ENTRYPOINT ["python", "docker/entrypoint.py"]
CMD ["streamlit", "run", "app/main.py", "--server.address", "0.0.0.0", "--server.port", "8501"]
