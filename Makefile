.PHONY: lint typecheck test

lint:
	ruff check .

typecheck:
	mypy skills scripts app.py

test:
	pytest -v
