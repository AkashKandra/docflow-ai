.PHONY: install test lint samples demo eval run

install:
	pip install -r requirements-dev.txt

test:
	python -m pytest -q

lint:
	ruff check .

samples:
	python scripts/generate_samples.py

demo: samples
	python scripts/demo.py

eval: samples
	DOCFLOW_MODE=mock DOCFLOW_LOCAL_DIR=.local_data/eval python scripts/evaluate.py --ext txt

run:
	cp -n local.settings.sample.json local.settings.json || true
	func start
