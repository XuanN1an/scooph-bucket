PYTHON ?= python

.PHONY: validate validate-urls

validate:
	$(PYTHON) scripts/validate.py

validate-urls:
	$(PYTHON) scripts/validate.py --check-urls
