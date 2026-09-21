FROM python:3.12-slim

# Sandbox image for untrusted repo code. No secrets baked in.
# NOTE: `pytest` is not a Debian package — install via pip instead.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential nodejs npm golang-go rustc cargo \
    default-jdk maven ruby php cmake make \
    jq bats shellcheck \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest
RUN useradd -m -u 10000 worker
USER worker
WORKDIR /workspace
CMD ["sh"]
