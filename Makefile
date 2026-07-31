.PHONY: setup validate build reproducible verify-routes

setup:
	python3 scripts/setup_pinned_theme.py

validate:
	python3 scripts/run_validation.py
	python3 scripts/validate_content.py
	python3 scripts/validate_preservation_baseline.py

build:
	python3 scripts/build_site.py

reproducible:
	python3 scripts/build_site.py --verify-reproducible

verify-routes:
	python3 scripts/verify_built_routes.py
