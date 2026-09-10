FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Non-root: this tier is the only publicly reachable one, so it gets no write
# access to its own image. The users file, if you use one, is mounted read-only.
RUN useradd --create-home --uid 10001 frontend
USER frontend

EXPOSE 8081
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
