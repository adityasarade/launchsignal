.PHONY: help install test doctor scan fast serve health review pond docker clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## create a venv and install the package
	python3 -m venv .venv && .venv/bin/pip install -q -e . && \
	  echo "installed. next: cp .env.example .env && make doctor"

test:  ## run the full test suite
	PYTHONPATH=src:tests python3 -m unittest discover -s tests -v

doctor:  ## validate setup without sending anything
	PYTHONPATH=src python3 -m launchsignal.cli doctor

scan:  ## run one full scan cycle
	PYTHONPATH=src python3 -m launchsignal.cli scan

fast:  ## run the founder fast lane once
	PYTHONPATH=src python3 -m launchsignal.cli fast

serve:  ## run on a schedule until stopped
	PYTHONPATH=src python3 -m launchsignal.cli serve

health:  ## print an operational report
	PYTHONPATH=src python3 -m launchsignal.cli health

review:  ## show candidates held for review
	PYTHONPATH=src python3 -m launchsignal.cli review

pond:  ## serve the Pond Protocol V1 control plane
	PYTHONPATH=src python3 -m launchsignal.cli pond

docker:  ## build and run with docker compose
	docker compose up --build monitor

clean:
	rm -rf .venv data __pycache__ src/**/__pycache__ tests/__pycache__
