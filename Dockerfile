# Production Dockerfile for Outlook Automation & Mail Server
# Uses Microsoft Playwright image containing all Linux graphical/browser libraries for headless Chromium
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8000

WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    xvfb \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    patchright install chromium

# Copy application source code
COPY . .

# Ensure runtime directories exist
RUN mkdir -p /app/OutlookManage/data /app/OutlookManage/logs /app/OutlookRegister/Results

EXPOSE 8000 18080

CMD ["python", "start.py", "--all", "--host", "0.0.0.0", "--port", "8000"]
