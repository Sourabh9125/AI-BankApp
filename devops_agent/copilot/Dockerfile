FROM python:3.12-slim

WORKDIR /app

# Copy source
COPY . .

# Install dependencies
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

ENV PYTHONPATH=/app/src

# Set entrypoint to watchdog by default
ENTRYPOINT ["python", "-m", "copilot.watchdog"]

# Optional: expose log port or health check
# EXPOSE 8000
