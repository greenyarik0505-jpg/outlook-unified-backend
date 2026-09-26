package geoip

import (
	"bufio"
	"context"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
	"testing"
	"time"
)

func TestRouterImmediateStopReleasesListener(t *testing.T) {
	previous := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(previous)
	for i := 0; i < 20; i++ {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		address := listener.Addr().String()
		port := uint16(listener.Addr().(*net.TCPAddr).Port)
		_ = listener.Close()
		router := NewRouter(RouterConfig{Listen: "127.0.0.1", Port: port}, log.New(io.Discard, "", 0))
		if err := router.Start(context.Background()); err != nil {
			t.Fatal(err)
		}
		if err := router.Stop(); err != nil {
			t.Fatal(err)
		}
		rebound, err := net.Listen("tcp", address)
		if err != nil {
			t.Fatalf("Stop returned before releasing the listener: %v", err)
		}
		_ = rebound.Close()
	}
}

type tunnelDialer struct{ conn net.Conn }

func (d tunnelDialer) DialContext(context.Context, string, string) (net.Conn, error) {
	return d.conn, nil
}

func TestRouterStopClosesBothTunnelDirections(t *testing.T) {
	target, peer := net.Pipe()
	defer peer.Close()
	router := NewRouter(RouterConfig{}, log.New(io.Discard, "", 0))
	router.SetGlobalPool(tunnelDialer{target})
	server := httptest.NewServer(router)
	defer server.Close()
	client, err := net.Dial("tcp", server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	_, _ = io.WriteString(client, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
	response, err := http.ReadResponse(bufio.NewReader(client), &http.Request{Method: http.MethodConnect})
	if err != nil || response.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT response=%v error=%v", response, err)
	}
	done := make(chan error, 1)
	go func() { done <- router.Stop() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		_ = peer.Close()
		<-done
		t.Fatal("Stop blocked on the upstream copy after closing the client")
	}
	if _, err := client.Read(make([]byte, 1)); err == nil {
		t.Fatal("client tunnel remains open")
	}
	_ = peer.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := peer.Read(make([]byte, 1)); err != io.EOF {
		t.Fatalf("upstream not closed: %v", err)
	}
	if err := router.Stop(); err != nil {
		t.Fatal(err)
	}
}

func TestRouterForwardsBufferedConnectPayload(t *testing.T) {
	target, peer := net.Pipe()
	defer peer.Close()
	_ = peer.SetDeadline(time.Now().Add(2 * time.Second))
	router := NewRouter(RouterConfig{}, log.New(io.Discard, "", 0))
	router.SetGlobalPool(tunnelDialer{target})
	defer router.Stop()
	server := httptest.NewServer(router)
	defer server.Close()
	client, err := net.Dial("tcp", server.Listener.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(2 * time.Second))
	echoDone := make(chan error, 1)
	go func() {
		defer peer.Close()
		buffer := make([]byte, 4)
		_, err := io.ReadFull(peer, buffer)
		if err == nil {
			_, err = peer.Write(buffer)
		}
		echoDone <- err
	}()
	_, _ = io.WriteString(client, "CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\nping")
	reader := bufio.NewReader(client)
	response, err := http.ReadResponse(reader, &http.Request{Method: http.MethodConnect})
	if err != nil || response.StatusCode != http.StatusOK {
		t.Fatalf("CONNECT response=%v error=%v", response, err)
	}
	buffer := make([]byte, 4)
	if _, err := io.ReadFull(reader, buffer); err != nil || string(buffer) != "ping" {
		t.Fatalf("buffered tunnel payload=%q error=%v", buffer, err)
	}
	if err := <-echoDone; err != nil {
		t.Fatal(err)
	}
}
