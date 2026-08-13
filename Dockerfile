FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEX_HOME=/home/centinela/.codex

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt \
    && CODEX_BIN="$(python -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())')" \
    && test -x "$CODEX_BIN" \
    && ln -s "$CODEX_BIN" /usr/local/bin/codex \
    && codex --version

COPY . .
RUN useradd --create-home --uid 10001 centinela \
    && install -d -m 0700 -o centinela -g centinela /home/centinela/.codex \
    && install -d -m 0750 -o centinela -g centinela \
        /app/data \
        /app/data/chroma \
        /app/data/codex-work \
        /app/reports

USER centinela
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
