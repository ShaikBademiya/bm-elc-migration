FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl

# CMD ["amf-pipeline"]
