.PHONY: check build e2e e2e-auto container-smoke
GO ?= go
check:
	$(GO) test -race ./...
	$(GO) vet ./...
	python3 -B -m unittest discover -s e2e
	python3 -B -m unittest discover -s skills/multica-bridge/scripts
	sh -n e2e/run.sh
	sh -n e2e/supervisor-smoke.sh

build:
	$(GO) build -buildvcs=false -o bin/multica-bridge ./cmd/multica-bridge

e2e:
	@sh e2e/run.sh smoke

e2e-auto:
	@sh e2e/run.sh verify

container-smoke:
	@sh e2e/supervisor-smoke.sh
