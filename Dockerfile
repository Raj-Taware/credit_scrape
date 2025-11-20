# Stage 1: Builder - Installs dependencies and Playwright browsers
FROM python:3.11-slim as builder

# Install core system dependencies needed by Playwright/Chromium runtime.
# This explicitly includes common missing libraries like libgbm1 and libasound2.
# We also run 'playwright install-deps' to handle any dynamic dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm1 \ 
    && \
    rm -rf /var/lib/apt/lists/*

# Set up working directory and install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Playwright Browser Installation (Crucial for stability) ---
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Use 'install' to get the binaries and 'install-deps' to verify
RUN playwright install chromium && \
    playwright install-deps

# Stage 2: Final Runtime - Creates a clean, lightweight final image
FROM python:3.11-slim
WORKDIR /app

# Copy installed Python packages and installed Playwright browsers/drivers
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /ms-playwright /ms-playwright

# Copy the app files
COPY scraper_api.py .

# Set environment path so the Playwright driver can find Chromium executable
ENV PATH="${PATH}:/ms-playwright/chromium" 
# Set LLM Key placeholder (Render overrides this securely)
ENV GEMINI_API_KEY="" 

EXPOSE 8000

# Command to run the application
CMD ["uvicorn", "scraper_api:app", "--host", "0.0.0.0", "--port", "8000"]