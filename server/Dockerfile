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

# uploads/ and config.json live on a mounted volume so they survive restarts
VOLUME ["/app/uploads", "/app/config.json"]

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
