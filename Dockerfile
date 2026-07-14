FROM python:3.11-slim

# Set environment variables
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies (including ffmpeg, curl, unzip, nodejs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    ffmpeg \
    unzip \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Deno globally (so it's available in PATH for any user running the container)
RUN curl -fsSL https://deno.land/x/install/install.sh | sh -s -- -d /usr/local/bin

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
