.PHONY: lint deps test

all: lint test

lint: deps
	pipenv run flake8 --ignore=E501 .
	pipenv run mypy --strict .
	pipenv run black --check .

deps:
	pipenv sync --dev

test: deps
	pipenv run pytest tests.py
