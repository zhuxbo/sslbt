.PHONY: build test clean lint release build-release check-agent-config finish-check docker-test docker-clean mock-api-test

build:
	@if [ -z "$(VERSION)" ]; then echo "用法: make build VERSION=1.0.0"; exit 1; fi
	bash scripts/build.sh $(VERSION)

test:
	python3 -m pytest tests/ -v -W error

test-cov:
	python3 -m pytest tests/ -v --cov=src/lib --cov-report=term-missing

lint:
	python3 -m flake8 src/ --max-line-length=120 --exclude=__pycache__
	python3 -m flake8 tests/ --max-line-length=120 --exclude=__pycache__

release:
	@echo "真实发布必须遵循 skills/remote-release.md；请使用对应薄工具入口。"
	@exit 1

build-release:
	@if [ -z "$(VERSION)" ]; then echo "用法: make build-release VERSION=1.0.0"; exit 1; fi
	bash build/release.sh --prepare $(VERSION)

check-agent-config:
	python3 scripts/check-agent-config.py

finish-check: test lint check-agent-config
	bash build/release.sh --dry-run 0.0.0-finish-check
	git diff --check

clean:
	rm -rf dist/*.zip
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name '*.pyc' -delete 2>/dev/null || true

# 收编进本仓的 docker-test 假服务端（Go）。Dockerfile 只 COPY main.go，
# 镜像内不含测试，故在宿主机用临时 module 跑；无 Go 工具链时跳过
mock-api-test:
	@command -v go >/dev/null 2>&1 || { echo "未安装 Go，跳过 mock-api 测试"; exit 0; }
	@tmp=$$(mktemp -d) && cp docker/mock-api/main.go docker/mock-api/main_test.go $$tmp/ && \
		cd $$tmp && go mod init mock-api >/dev/null 2>&1 && go test ./... ; \
		rc=$$?; rm -rf $$tmp; exit $$rc

docker-test:
	cd docker && docker compose up -d --build && bash scripts/run-tests.sh --all

docker-clean:
	cd docker && docker compose down -v
