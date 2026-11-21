// Loopback-only TCP forwarder for the isolated E2E Compose stack.
// Keeps postgres, backend, frontend, and runtime on an internal:true network
// while exposing only backend:8080 and frontend:3000 to host loopback.
package main

import (
	"context"
	"io"
	"log"
	"net"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	routes := [][2]string{
		{":18080", "backend:8080"},
		{":13000", "frontend:3000"},
	}

	var wg sync.WaitGroup
	listeners := make([]net.Listener, 0, len(routes))
	for _, r := range routes {
		ln, err := net.Listen("tcp", r[0])
		if err != nil {
			log.Fatalf("listen %s: %v", r[0], err)
		}
		listeners = append(listeners, ln)
		wg.Add(1)
		go func(ln net.Listener, dst string) {
			defer wg.Done()
			serve(ctx, ln, dst)
		}(ln, r[1])
	}

	<-ctx.Done()
	for _, ln := range listeners {
		_ = ln.Close()
	}
	wg.Wait()
}

func serve(ctx context.Context, ln net.Listener, dst string) {
	for {
		src, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			continue
		}
		go forward(ctx, src, dst)
	}
}

func forward(ctx context.Context, src net.Conn, dst string) {
	defer src.Close()
	dialer := net.Dialer{Timeout: 5 * time.Second}
	up, err := dialer.DialContext(ctx, "tcp", dst)
	if err != nil {
		return
	}
	defer up.Close()

	done := make(chan struct{}, 2)
	cp := func(w, r net.Conn) {
		_, _ = io.Copy(w, r)
		if tc, ok := w.(*net.TCPConn); ok {
			_ = tc.CloseWrite()
		}
		done <- struct{}{}
	}
	go cp(up, src)
	go cp(src, up)
	select {
	case <-done:
		<-done
	case <-ctx.Done():
	}
}
