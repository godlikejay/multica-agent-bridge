# Official backend image also contains /app/multica (pinned source Dockerfile).
FROM ghcr.io/multica-ai/multica-backend:v0.11.1 AS upstream
FROM golang:1.24.13-alpine AS builder
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -buildvcs=false -trimpath -o /multica-bridge ./cmd/multica-bridge
FROM alpine:3.21
RUN apk add --no-cache ca-certificates git
COPY --from=upstream /app/multica /usr/local/bin/multica
COPY --from=builder /multica-bridge /usr/local/bin/multica-bridge
ENV HOME=/home/runtime BRIDGE_ROOT=/bridge MULTICA_HERMES_PATH=/usr/local/bin/multica-bridge MULTICA_DAEMON_MAX_CONCURRENT_TASKS=4
RUN addgroup -g 10001 bridge \
 && adduser -D -H -u 10001 -G bridge bridge \
 && mkdir -p /home/runtime /bridge/inbox /bridge/outbox /bridge/state /workspaces \
 && chown -R 10001:10001 /home/runtime /bridge /workspaces
WORKDIR /workspaces
USER 10001:10001
ENTRYPOINT ["multica"]
CMD ["--profile", "bridge", "daemon", "start", "--foreground", "--no-auto-update", "--no-auto-reload"]
