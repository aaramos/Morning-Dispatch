FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY backend ./backend

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", "8000"]
