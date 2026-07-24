FROM python:3.12-slim

WORKDIR /app

# System deps for matplotlib/Pillow and cartopy/pyproj/shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
        libfreetype6 \
        libpng16-16 \
        fonts-dejavu-core \
        libproj-dev \
        proj-data \
        proj-bin \
        libgeos-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# uploads/ lives on a mounted volume so it survives restarts.
# /app/config is a bind-mounted directory (see compose) holding config.json.
# Mounting the directory (not the single file) lets _save_config() do its
# atomic tmp->config.json rename, which fails with EBUSY across a file mount.
VOLUME ["/app/uploads"]

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
