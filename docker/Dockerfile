FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git docker.io nodejs npm && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e . || pip install --no-cache-dir .
ENV WORKSPACES_ROOT=/workspaces DATABASE_URL=sqlite:////workspaces/contributor.db
CMD ["contributor", "--help"]
