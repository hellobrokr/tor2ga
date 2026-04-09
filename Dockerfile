FROM python:3.12-slim

WORKDIR /app

COPY core/requirements.txt requirements-core.txt
RUN pip install --no-cache-dir -r requirements-core.txt

RUN pip install --no-cache-dir fastapi uvicorn httpx stripe

COPY core/ ./core/
COPY site/ ./site/

WORKDIR /app/core

EXPOSE 8420

CMD ["python", "server.py"]
