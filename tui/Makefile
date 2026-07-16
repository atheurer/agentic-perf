GO ?= go
BINARY := aptui
DIST := dist

# Build flags
LDFLAGS := -s -w
CGO_ENABLED := 0

# Cross-compile targets
PLATFORMS := linux/amd64 linux/arm64 darwin/amd64 darwin/arm64

.PHONY: build check fmt vet test cross clean

build:
	CGO_ENABLED=$(CGO_ENABLED) $(GO) build -ldflags '$(LDFLAGS)' -o $(BINARY) ./cmd/aptui

check: fmt vet test

fmt:
	@test -z "$$($(GO) fmt ./...)" || (echo "gofmt: files need formatting" && exit 1)

vet:
	$(GO) vet ./...

test:
	$(GO) test ./...

cross: $(DIST)
	@for platform in $(PLATFORMS); do \
		os=$${platform%/*}; \
		arch=$${platform#*/}; \
		output=$(DIST)/$(BINARY)-$$os-$$arch; \
		echo "Building $$output"; \
		CGO_ENABLED=$(CGO_ENABLED) GOOS=$$os GOARCH=$$arch \
			$(GO) build -ldflags '$(LDFLAGS)' -o $$output ./cmd/aptui; \
	done

$(DIST):
	mkdir -p $(DIST)

clean:
	rm -f $(BINARY)
	rm -rf $(DIST)
