package geoip

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

// RouterConfig holds configuration for the GeoIP router
type RouterConfig struct {
	Listen   string
	Port     uint16
	Username string
	Password string
}

// PoolDialer is an interface for dialing through a specific pool
type PoolDialer interface {
	DialContext(ctx context.Context, network, address string) (net.Conn, error)
}

// Router handles HTTP proxy requests with path-based region routing
type Router struct {
	cfg         RouterConfig
	pools       map[string]PoolDialer          // region -> dialer
	global      PoolDialer                     // default pool for requests without region path
	transports  map[PoolDialer]*http.Transport // cached transports per dialer
	server      *http.Server
	listener    net.Listener
	serveDone   chan struct{}
	mu          sync.RWMutex
	logger      *log.Logger
	cancel      context.CancelFunc
	stopOnce    sync.Once
	stopDone    chan struct{}
	stopErr     error
	stopping    bool
	connections map[net.Conn]struct{}
	connWG      sync.WaitGroup
}

// NewRouter creates a new GeoIP router
func NewRouter(cfg RouterConfig, logger *log.Logger) *Router {
	if logger == nil {
		logger = log.Default()
	}
	return &Router{
		cfg:         cfg,
		pools:       make(map[string]PoolDialer),
		transports:  make(map[PoolDialer]*http.Transport),
		logger:      logger,
		stopDone:    make(chan struct{}),
		connections: make(map[net.Conn]struct{}),
	}
}

// SetPool registers a pool dialer for a specific region
func (r *Router) SetPool(region string, dialer PoolDialer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.pools[region] = dialer
	r.closeTransportsLocked()
}

// SetGlobalPool sets the default pool for requests without region path
func (r *Router) SetGlobalPool(dialer PoolDialer) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.global = dialer
	r.closeTransportsLocked()
}

// Start starts the GeoIP router HTTP server
func (r *Router) Start(ctx context.Context) error {
	if ctx == nil {
		ctx = context.Background()
	}
	addr := fmt.Sprintf("%s:%d", r.cfg.Listen, r.cfg.Port)
	lifecycleCtx, cancel := context.WithCancel(ctx)

	r.mu.Lock()
	if r.stopping || r.server != nil {
		r.mu.Unlock()
		cancel()
		return fmt.Errorf("geoip router is already stopped or started")
	}
	r.server = &http.Server{
		Addr:        addr,
		Handler:     r,
		BaseContext: func(net.Listener) context.Context { return lifecycleCtx },
	}
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		r.server = nil
		r.mu.Unlock()
		cancel()
		return err
	}
	r.cancel = cancel
	r.listener = listener
	r.serveDone = make(chan struct{})
	serveDone := r.serveDone
	srv := r.server
	r.mu.Unlock()

	go func() {
		defer close(serveDone)
		r.logger.Printf("🌐 GeoIP Router started on %s", addr)
		r.logger.Println("   Routes: /jp, /kr, /us, /hk, /tw, /sg, /other (default: all nodes)")
		if err := srv.Serve(listener); err != nil && err != http.ErrServerClosed {
			r.logger.Printf("GeoIP router error: %v", err)
		}
	}()

	go func() {
		<-lifecycleCtx.Done()
		_ = r.Stop()
	}()

	return nil
}

// Stop stops the GeoIP router
func (r *Router) Stop() error {
	r.stopOnce.Do(func() {
		r.mu.Lock()
		r.stopping = true
		cancel := r.cancel
		srv := r.server
		listener, serveDone := r.listener, r.serveDone
		r.closeTransportsLocked()
		connections := make([]net.Conn, 0, len(r.connections))
		for conn := range r.connections {
			connections = append(connections, conn)
		}
		r.mu.Unlock()
		if cancel != nil {
			cancel()
		}
		for _, conn := range connections {
			_ = conn.Close()
		}
		if srv != nil {
			ctx, cancelShutdown := context.WithTimeout(context.Background(), 5*time.Second)
			r.stopErr = srv.Shutdown(ctx)
			cancelShutdown()
			if r.stopErr != nil {
				_ = srv.Close()
			}
		}
		if listener != nil {
			_ = listener.Close()
			<-serveDone
		}
		r.connWG.Wait()
		close(r.stopDone)
	})
	<-r.stopDone
	return r.stopErr
}

func (r *Router) registerConnection(conn net.Conn) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.stopping {
		return false
	}
	r.connections[conn] = struct{}{}
	r.connWG.Add(1)
	return true
}

func (r *Router) unregisterConnection(conn net.Conn) {
	r.mu.Lock()
	delete(r.connections, conn)
	r.mu.Unlock()
	r.connWG.Done()
}

func (r *Router) closeTransportsLocked() {
	for _, transport := range r.transports {
		transport.CloseIdleConnections()
	}
	clear(r.transports)
}

// checkProxyAuth validates the Proxy-Authorization header.
// Proxy clients send credentials via "Proxy-Authorization", not "Authorization".
func (r *Router) checkProxyAuth(req *http.Request) bool {
	auth := req.Header.Get("Proxy-Authorization")
	if auth == "" {
		return false
	}
	const prefix = "Basic "
	if !strings.HasPrefix(auth, prefix) {
		return false
	}
	decoded, err := base64.StdEncoding.DecodeString(auth[len(prefix):])
	if err != nil {
		return false
	}
	parts := strings.SplitN(string(decoded), ":", 2)
	if len(parts) != 2 {
		return false
	}
	return parts[0] == r.cfg.Username && parts[1] == r.cfg.Password
}

// ServeHTTP handles incoming HTTP proxy requests
func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	// Check proxy authentication if configured
	if r.cfg.Username != "" {
		if !r.checkProxyAuth(req) {
			w.Header().Set("Proxy-Authenticate", `Basic realm="Proxy"`)
			http.Error(w, "Proxy authentication required", http.StatusProxyAuthRequired)
			return
		}
	}

	// Extract region from path
	region, targetHost := r.parseRequest(req)

	// Get the appropriate pool
	r.mu.RLock()
	var dialer PoolDialer
	if region != "" {
		dialer = r.pools[region]
	}
	if dialer == nil {
		dialer = r.global
	}
	r.mu.RUnlock()

	if dialer == nil {
		http.Error(w, "No proxy pool available", http.StatusServiceUnavailable)
		return
	}

	if req.Method == http.MethodConnect {
		r.handleConnect(w, req, dialer, targetHost)
	} else {
		r.handleHTTP(w, req, dialer, targetHost)
	}
}

// parseRequest extracts region and target host from the request
func (r *Router) parseRequest(req *http.Request) (region, targetHost string) {
	// For CONNECT requests, the host is in req.Host
	// For regular requests, check the path prefix

	if req.Method == http.MethodConnect {
		// CONNECT requests: check if host starts with region prefix
		// e.g., CONNECT jp/example.com:443 or just example.com:443
		host := req.Host
		for _, reg := range AllRegions() {
			prefix := reg + "/"
			if strings.HasPrefix(host, prefix) {
				return reg, strings.TrimPrefix(host, prefix)
			}
		}
		return "", host
	}

	// For regular HTTP requests, check URL path
	path := req.URL.Path
	for _, reg := range AllRegions() {
		prefix := "/" + reg + "/"
		if strings.HasPrefix(path, prefix) {
			// Rewrite the path
			req.URL.Path = "/" + strings.TrimPrefix(path, prefix)
			return reg, req.Host
		}
		// Also check for exact match like /jp
		if path == "/"+reg {
			req.URL.Path = "/"
			return reg, req.Host
		}
	}

	return "", req.Host
}

// handleConnect handles HTTPS CONNECT tunneling
func (r *Router) handleConnect(w http.ResponseWriter, req *http.Request, dialer PoolDialer, targetHost string) {
	ctx, cancel := context.WithTimeout(req.Context(), 30*time.Second)
	defer cancel()

	targetConn, err := dialer.DialContext(ctx, "tcp", targetHost)
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to connect: %v", err), http.StatusBadGateway)
		return
	}
	defer targetConn.Close()

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "Hijacking not supported", http.StatusInternalServerError)
		return
	}

	clientConn, buffered, err := hijacker.Hijack()
	if err != nil {
		http.Error(w, fmt.Sprintf("Hijack failed: %v", err), http.StatusInternalServerError)
		return
	}
	if !r.registerConnection(clientConn) {
		_ = clientConn.Close()
		return
	}
	defer r.unregisterConnection(clientConn)
	defer clientConn.Close()
	if !r.registerConnection(targetConn) {
		return
	}
	defer r.unregisterConnection(targetConn)

	// Send 200 Connection Established
	clientConn.Write([]byte("HTTP/1.1 200 Connection Established\r\n\r\n"))

	// Bidirectional copy
	var wg sync.WaitGroup
	wg.Add(2)

	go func() {
		defer wg.Done()
		defer targetConn.Close()
		defer clientConn.Close()
		_, _ = io.Copy(targetConn, buffered)
	}()

	go func() {
		defer wg.Done()
		defer targetConn.Close()
		defer clientConn.Close()
		_, _ = io.Copy(clientConn, targetConn)
	}()

	wg.Wait()
}

// getTransport returns a cached http.Transport for the given dialer, creating one if needed.
func (r *Router) getTransport(dialer PoolDialer) *http.Transport {
	r.mu.RLock()
	t, ok := r.transports[dialer]
	r.mu.RUnlock()
	if ok {
		return t
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	// Double-check after acquiring write lock
	if t, ok = r.transports[dialer]; ok {
		return t
	}
	t = &http.Transport{
		DialContext:         dialer.DialContext,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 10,
		IdleConnTimeout:     90 * time.Second,
		DisableKeepAlives:   r.stopping,
	}
	r.transports[dialer] = t
	return t
}

// handleHTTP handles regular HTTP requests
func (r *Router) handleHTTP(w http.ResponseWriter, req *http.Request, dialer PoolDialer, targetHost string) {
	ctx, cancel := context.WithTimeout(req.Context(), 30*time.Second)
	defer cancel()

	// Create a new request to the target
	targetURL := req.URL
	if targetURL.Host == "" {
		targetURL.Host = targetHost
	}
	if targetURL.Scheme == "" {
		targetURL.Scheme = "http"
	}

	outReq, err := http.NewRequestWithContext(ctx, req.Method, targetURL.String(), req.Body)
	if err != nil {
		http.Error(w, fmt.Sprintf("Failed to create request: %v", err), http.StatusInternalServerError)
		return
	}

	// Copy headers
	for key, values := range req.Header {
		for _, value := range values {
			outReq.Header.Add(key, value)
		}
	}

	// Remove hop-by-hop headers
	outReq.Header.Del("Proxy-Connection")
	outReq.Header.Del("Proxy-Authorization")

	// Use cached transport with connection pooling
	transport := r.getTransport(dialer)

	resp, err := transport.RoundTrip(outReq)
	if err != nil {
		http.Error(w, fmt.Sprintf("Request failed: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	// Copy response headers
	for key, values := range resp.Header {
		for _, value := range values {
			w.Header().Add(key, value)
		}
	}

	w.WriteHeader(resp.StatusCode)
	io.Copy(w, resp.Body)
}
