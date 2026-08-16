FROM python:3.11-slim

WORKDIR /app

# Arena MP4 replay is rendered inside the bot container. Keep the binary
# minimal and remove apt metadata so the image does not retain package cache.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
ENV PYTHONPATH=/app

CMD ["python", "bot/main.py"]

