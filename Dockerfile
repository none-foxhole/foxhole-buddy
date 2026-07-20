# --- build stage: install deps into an isolated prefix, then discard pip/caches ---
FROM python:3.12-alpine AS build

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- runtime stage: just the interpreter + installed packages + app code ---
FROM python:3.12-alpine

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Bring in only the installed packages from the build stage (no pip, no wheels).
COPY --from=build /install /usr/local

COPY . .

# DB, catalog cache and war cache all live here; mounted as a volume so they
# persist across restarts and rebuilds.
VOLUME ["/app/data"]

CMD ["python3", "main.py"]
