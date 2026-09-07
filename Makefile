.PHONY: dev test up down

dev:
	pip install -e .[dev,gmail]

test:
	pytest tests/unit tests/integration

up:
	docker compose up -d

down:
	docker compose down
