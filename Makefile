.PHONY: setup validate build reproducible

setup:
	python3 scripts/setup_pinned_theme.py

validate:
	python3 scripts/run_validation.py

build:
	python3 scripts/build_site.py

reproducible:
	python3 scripts/build_site.py --verify-reproducible
