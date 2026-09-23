FROM python:3.12-slim

# Sandbox image for untrusted repo code. No secrets baked in.
# Contains build toolchains for the supported ecosystems plus the bootstrap
# prerequisites (rustup, uv, corepack). Exact toolchain versions are installed
# on demand by the deterministic EnvironmentBootstrapper into a writable,
# cacheable env volume; OpenCode itself is mounted read-only at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git build-essential curl ca-certificates pkg-config libssl-dev \
    nodejs npm golang-go \
    default-jdk maven ruby php cmake make \
    jq bats shellcheck unzip \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest uv

# rustup toolchain manager (no default toolchain: the bootstrapper installs the
# repository's declared channel into RUSTUP_HOME). The distro rustc is
# deliberately absent so rustup owns toolchains.
RUN mkdir -p /usr/local/cargo /usr/local/rustup \
  && CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup RUSTUP_INIT_SKIP_PATH_CHECK=yes \
     sh -c "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none --no-modify-path" \
  && chmod -R a+rx /usr/local/cargo /usr/local/rustup

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN useradd -m -u 10000 worker || true
WORKDIR /workspace
CMD ["sh"]
