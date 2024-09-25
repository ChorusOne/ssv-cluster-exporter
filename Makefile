.PHONY: lint deps test test-verbose

all: lint test

lint: deps
	pipenv run flake8 --ignore=E501,W503 .
	pipenv run mypy --strict .
	pipenv run black --check .

deps:
	pipenv sync --dev

test: deps
	pipenv run pytest tests.py -sv

test-verbose: deps
	pipenv run pytest tests.py -v -o log_cli=true --capture=fd --show-capture=stderr --log-level=DEBUG
