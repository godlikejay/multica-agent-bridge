FROM golang:1.24.13-alpine AS builder
WORKDIR /src
COPY main.go ./
RUN CGO_ENABLED=0 go build -buildvcs=false -trimpath -o /e2e-proxy main.go

FROM scratch
COPY --from=builder /e2e-proxy /e2e-proxy
USER 65532:65532
ENTRYPOINT ["/e2e-proxy"]
