.PHONY: help install test dry-run collect generate approve publish analytics verify-sources lint clean

help:
	@echo "make install         — установить зависимости"
	@echo "make test            — прогнать тесты"
	@echo "make dry-run         — полный сухой прогон пайплайна (без сети в Telegram)"
	@echo "make verify-sources  — проверить все источники HTTP-запросом"
	@echo "make collect         — только сбор источников"
	@echo "make generate        — сбор + генерация постов"
	@echo "make approve         — опрос кнопок апрува (long-poll)"
	@echo "make publish         — публикация одобренного по слотам"
	@echo "make analytics       — недельный отчёт"

install:
	python -m pip install -r requirements.txt

test:
	python -m pytest -q

dry-run:
	DRY_RUN=1 python -m pipeline.run --dry-run

collect:
	python -m pipeline.run --stage collect

generate:
	python -m pipeline.run --stage generate

approve:
	python -m pipeline.run --stage approve

publish:
	python -m pipeline.run --stage publish

analytics:
	python -m pipeline.run --stage analytics

verify-sources:
	python -m pipeline.verify_sources

clean:
	rm -rf out assets/generated .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
