.PHONY: check build e2e e2e-auto
GO ?= go
check:
	$(GO) test -race ./...
	$(GO) vet ./...
	python3 -B e2e/test_fixture.py
	python3 -B e2e/test_seed.py
	python3 -B skills/multica-bridge/scripts/test_mailbox.py
	sh -n e2e/run.sh

build:
	$(GO) build -buildvcs=false -o bin/multica-bridge ./cmd/multica-bridge

e2e:
	@sh e2e/run.sh smoke

e2e-auto:
	@sh e2e/run.sh verify
