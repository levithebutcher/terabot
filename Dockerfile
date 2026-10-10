FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg for video thumbnails, megatools and official megacmd for Mega)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    megatools \
    && curl -fsSL https://mega.nz/linux/repo/Debian_12/amd64/megacmd-Debian_12_amd64.deb -o /tmp/megacmd.deb \
    && (apt-get install -y --no-install-recommends /tmp/megacmd.deb || true) \
    && rm -f /tmp/megacmd.deb \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency requirements
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Hugging Face Spaces requires running as a non-root user (UID 1000)
RUN useradd -m -u 1000 user
# Give the user ownership of the /app directory so it can write DB and downloads
RUN chown -R user:user /app
USER user

# Create downloads directory explicitly
RUN mkdir -p /app/downloads

# Expose standard cloud port
EXPOSE 8080 10000 7860

# Run the bot
CMD ["python", "bot.py"]
