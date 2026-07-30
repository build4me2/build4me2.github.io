.PHONY: setup validate build reproducible verify-routes

setup:
	python3 scripts/setup_pinned_theme.py

validate:
	python3 scripts/run_validation.py

build:
	python3 scripts/build_site.py

reproducible:
	python3 scripts/build_site.py --verify-reproducible

verify-routes:
	python3 scripts/verify_built_routes.py
