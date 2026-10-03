.PHONY: install compile test gate run

install:
	python -m pip install -r requirements.txt

compile:
	python -m compileall -q app tests

test:
	python -m pytest -q

gate:
	python scripts/release_gate.py --local

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000
