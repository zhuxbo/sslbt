.PHONY: build test clean lint release

build:
	@if [ -z "$(VERSION)" ]; then echo "用法: make build VERSION=1.0.0"; exit 1; fi
	bash scripts/build.sh $(VERSION)

test:
	python3 -m pytest tests/ -v

test-cov:
	python3 -m pytest tests/ -v --cov=src/lib --cov-report=term-missing

lint:
	python3 -m flake8 src/ --max-line-length=120 --exclude=__pycache__

release:
	@if [ -z "$(VERSION)" ]; then echo "用法: make release VERSION=1.0.0"; exit 1; fi
	bash build/release.sh $(VERSION)

clean:
	rm -rf dist/*.zip
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true

docker-test:
	cd docker && docker compose up -d --build && bash scripts/run-tests.sh --all

docker-clean:
	cd docker && docker compose down -v
