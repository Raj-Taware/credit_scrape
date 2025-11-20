# Stage 1: Builder - Installs dependencies and Playwright browsers
FROM python:3.11-slim as builder

# Install system dependencies needed for Playwright (Chromium)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 && \
    rm -rf /var/lib/apt/lists/*

# Set up environment
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Playwright Browser Installation (Crucial for free tier stability) ---
# This installs Chromium specifically, needed for the harvester
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

# Stage 2: Final Runtime - Creates a lightweight final image
FROM python:3.11-slim
WORKDIR /app

# Copy dependencies and installed browsers from the builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
# Copy the installed Chromium browser files
COPY --from=builder /ms-playwright /ms-playwright

# Copy the app files
COPY scraper_api.py .

# Set environment variable for Playwright and the LLM key
ENV PATH="${PATH}:/ms-playwright/chromium" 
ENV GEMINI_API_KEY="" 

EXPOSE 8000

# Command to run the application (Render will use this)
CMD ["uvicorn", "scraper_api:app", "--host", "0.0.0.0", "--port", "8000"]