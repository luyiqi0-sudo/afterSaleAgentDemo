FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY . .
CMD ["python", "-m", "uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
