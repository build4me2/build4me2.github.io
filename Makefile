.PHONY: setup validate build reproducible

setup:
	python3 scripts/setup_pinned_theme.py

validate:
	python3 -m unittest discover -s tests -p 'test_*.py'

build:
	python3 scripts/build_site.py

reproducible:
	python3 scripts/build_site.py --verify-reproducible
