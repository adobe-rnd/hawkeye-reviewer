FROM python:3.12-slim

# openssl is required for GitHub App JWT signing (RS256)
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ ./scripts/

# Run as non-root
RUN useradd -r -u 1001 -g root botuser
USER botuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

ENTRYPOINT ["python3", "scripts/webhook_server.py"]
