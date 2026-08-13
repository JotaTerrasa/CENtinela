.PHONY: install install-runtime run test lint audit compile docker-up docker-ollama validate-compose

install:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements-dev.txt

install-runtime:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.txt

run:
	.venv/bin/streamlit run app.py

test:
	.venv/bin/python -m pytest

lint:
	.venv/bin/ruff check .

audit:
	.venv/bin/pip-audit -r requirements.txt --progress-spinner off

compile:
	.venv/bin/python -m compileall -q app.py core scrapers agent rag scripts

docker-up:
	docker compose up --build

docker-ollama:
	docker compose -f docker-compose.yml -f docker-compose.ollama.yml up --build

validate-compose:
	docker compose config --quiet
	docker compose -f docker-compose.yml -f docker-compose.ollama.yml config --quiet
	docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.ollama-gpu.yml config --quiet
