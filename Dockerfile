FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (git for cloning repos, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications and install
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and install project in editable/package mode
COPY . .
RUN pip install --no-cache-dir -e .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn edl.api:app --host 0.0.0.0 --port ${PORT:-8000}"]

