.PHONY: install lint test run-api run-web up down seed

install:
	python -m pip install -e ".[dev]"

lint:
	ruff check .

test:
	pytest

run-api:
	uvicorn apps.api.main:app --reload --port 8000

run-web:
	streamlit run apps/web/app.py --server.port 8501

up:
	docker compose up --build -d

down:
	docker compose down

seed:
	python data/seed/generate_seed.py

