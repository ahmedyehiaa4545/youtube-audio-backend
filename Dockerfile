FROM python:3.11-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (including ffmpeg, curl, nodejs, libgomp1)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
    nodejs \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy the Deno binary directly from the official Deno image
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

# Copy and install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install latest nightly version of yt-dlp
RUN pip install -U --pre "yt-dlp[default]"

# Copy application files
COPY . .

# Create public directory
RUN mkdir -p public

# Expose port
EXPOSE 8000

# Run uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
