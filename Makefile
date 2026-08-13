.PHONY: install install-runtime run test compile docker-up

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

compile:
	.venv/bin/python -m compileall -q app.py core scrapers agent rag

docker-up:
	docker compose up --build
