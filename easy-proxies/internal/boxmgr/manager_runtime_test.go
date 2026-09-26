package boxmgr

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"easy_proxies/internal/config"
	"easy_proxies/internal/monitor"
)

func TestVerifyRuntimeChecksEveryConfiguredPort(t *testing.T) {
	first, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer first.Close()
	second, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	firstPort := uint16(first.Addr().(*net.TCPAddr).Port)
	secondPort := uint16(second.Addr().(*net.TCPAddr).Port)
	manager := &Manager{cfg: &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "0.0.0.0"},
		Nodes: []config.NodeConfig{
			{Name: "first", Port: firstPort},
			{Name: "second", Port: secondPort},
		},
	}}
	if err := manager.VerifyRuntime(context.Background()); err != nil {
		t.Fatalf("VerifyRuntime() with all ports open: %v", err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
	err = manager.VerifyRuntime(context.Background())
	if err == nil || !strings.Contains(err.Error(), fmt.Sprint(secondPort)) {
		t.Fatalf("VerifyRuntime() error = %v, want missing port %d", err, secondPort)
	}
}

func TestVerifyRuntimeHonorsCanceledContext(t *testing.T) {
	manager := &Manager{cfg: &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "192.0.2.1"},
		Nodes:     []config.NodeConfig{{Name: "canceled", Port: 443}},
	}}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	started := time.Now()
	err := manager.VerifyRuntime(ctx)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("VerifyRuntime() error = %v, want context.Canceled", err)
	}
	if elapsed := time.Since(started); elapsed > 250*time.Millisecond {
		t.Fatalf("VerifyRuntime() cancellation took %v", elapsed)
	}
}

func TestVerifyRuntimeRetriesPortsThatBecomeReady(t *testing.T) {
	port := freePort(t)
	manager := &Manager{cfg: &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1"},
		Nodes:     []config.NodeConfig{{Name: "delayed", Port: port}},
	}}

	listenerReady := make(chan net.Listener, 1)
	go func() {
		time.Sleep(500 * time.Millisecond)
		listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", fmt.Sprint(port)))
		if err == nil {
			listenerReady <- listener
		}
		close(listenerReady)
	}()

	err := manager.VerifyRuntime(context.Background())
	listener := <-listenerReady
	if listener != nil {
		defer listener.Close()
	}
	if err != nil {
		t.Fatalf("VerifyRuntime() rejected a port that became ready: %v", err)
	}
}

func TestReassignConflictingPortSkipsConsecutiveOccupiedPorts(t *testing.T) {
	cfg := &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1"},
		Nodes: []config.NodeConfig{
			{Name: "conflict", Port: 30000},
			{Name: "used", Port: 30003},
		},
	}
	available := func(_ string, port uint16) bool {
		return port != 30001 && port != 30002
	}
	if !reassignConflictingPortWithAvailability(cfg, 30000, available) {
		t.Fatal("reassignConflictingPortWithAvailability() returned false")
	}
	if got := cfg.Nodes[0].Port; got != 30004 {
		t.Fatalf("reassigned port = %d, want 30004", got)
	}
}

func TestReassignConflictingPortDoesNotOverflowAtMaxPort(t *testing.T) {
	cfg := &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1"},
		Nodes:     []config.NodeConfig{{Name: "max", Port: 65535}},
	}
	if reassignConflictingPortWithAvailability(cfg, 65535, func(string, uint16) bool { return true }) {
		t.Fatal("reassignConflictingPortWithAvailability() reassigned beyond port 65535")
	}
	if got := cfg.Nodes[0].Port; got != 65535 {
		t.Fatalf("port changed after failed reassignment: %d", got)
	}
}

func TestRebuildPortAssignmentsSkipsExternallyOccupiedPort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	occupied := uint16(listener.Addr().(*net.TCPAddr).Port)
	cfg := &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1", BasePort: occupied},
		Nodes:     []config.NodeConfig{{Name: "node", URI: "socks5://127.0.0.1:1"}},
	}
	manager := New(cfg, monitor.Config{})
	if err := manager.RebuildPortAssignments(); err != nil {
		t.Fatalf("RebuildPortAssignments() error = %v", err)
	}
	if got := cfg.Nodes[0].Port; got == 0 || got == occupied {
		t.Fatalf("assigned port = %d, occupied port = %d", got, occupied)
	}
}

func TestNextAvailablePortReturnsZeroWhenPortRangeIsExhausted(t *testing.T) {
	manager := &Manager{cfg: &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1", BasePort: 65535},
		Nodes:     []config.NodeConfig{{Name: "max", Port: 65535}},
	}}
	manager.mu.Lock()
	got := manager.nextAvailablePortLocked()
	manager.mu.Unlock()
	if got != 0 {
		t.Fatalf("nextAvailablePortLocked() = %d, want 0 when exhausted", got)
	}
}

func TestRebuildPortAssignmentsPersistsPorts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.yaml")
	cfg := &config.Config{
		Mode:      "multi-port",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1", BasePort: freePort(t)},
		Nodes: []config.NodeConfig{
			{Name: "first", URI: "socks5://127.0.0.1:1", Port: 25000, Source: config.NodeSourceInline},
			{Name: "second", URI: "socks5://127.0.0.1:2", Port: 25001, Source: config.NodeSourceInline},
		},
	}
	cfg.SetFilePath(path)
	if err := cfg.SaveFull(); err != nil {
		t.Fatalf("SaveFull() error = %v", err)
	}
	manager := New(cfg, monitor.Config{})
	if err := manager.RebuildPortAssignments(); err != nil {
		t.Fatalf("RebuildPortAssignments() error = %v", err)
	}
	loaded, err := config.Load(path)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if len(loaded.Nodes) != 2 || loaded.Nodes[0].Port != cfg.Nodes[0].Port || loaded.Nodes[1].Port != cfg.Nodes[1].Port {
		t.Fatalf("loaded ports = %#v, runtime ports = %#v", loaded.Nodes, cfg.Nodes)
	}
}

func TestStartAllowsEmptyNodePool(t *testing.T) {
	cfg := &config.Config{
		Mode:      "multi-port",
		LogLevel:  "error",
		MultiPort: config.MultiPortConfig{Address: "127.0.0.1", BasePort: 24000},
	}
	manager := New(cfg, monitor.Config{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := manager.Start(ctx); err != nil {
		t.Fatalf("Start() with empty pool: %v", err)
	}
	defer manager.Close()
	manager.mu.RLock()
	instance := manager.currentBox
	runtimeCfg := cloneConfig(manager.runtimeCfg)
	manager.mu.RUnlock()
	if instance != nil || runtimeCfg == nil || len(runtimeCfg.Nodes) != 0 {
		t.Fatalf("currentBox=%v runtimeCfg=%#v, want management-only runtime", instance, runtimeCfg)
	}
}

func TestIncrementalMultiPortReconcileKeepsBoxRunning(t *testing.T) {
	if os.Getenv("EASY_PROXIES_RUNTIME_TEST") != "1" {
		t.Skip("set EASY_PROXIES_RUNTIME_TEST=1 to run sing-box integration test")
	}
	firstPort := freePort(t)
	secondPort := freePort(t)
	for secondPort == firstPort {
		secondPort = freePort(t)
	}
	clashAPIPort := freePort(t)
	t.Setenv("EASY_PROXIES_CLASH_API_LISTEN", net.JoinHostPort("127.0.0.1", fmt.Sprint(clashAPIPort)))
	cfg := &config.Config{
		Mode:       "multi-port",
		LogLevel:   "error",
		MultiPort:  config.MultiPortConfig{Address: "127.0.0.1", BasePort: firstPort},
		Pool:       config.PoolConfig{Mode: "balance"},
		Management: config.ManagementConfig{},
		Nodes: []config.NodeConfig{{
			Name: "runtime-a", URI: "socks5://127.0.0.1:1", Port: firstPort, Source: config.NodeSourceInline,
		}},
	}
	cfg.SetFilePath(filepath.Join(t.TempDir(), "config.yaml"))
	if err := cfg.SaveFull(); err != nil {
		t.Fatal(err)
	}

	manager := New(cfg, monitor.Config{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := manager.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer manager.Close()
	manager.mu.RLock()
	initialBox := manager.currentBox
	manager.mu.RUnlock()
	assertListening(t, firstPort, true)

	if _, err := manager.CreateNode(ctx, config.NodeConfig{
		Name: "runtime-b", URI: "socks5://127.0.0.1:2", Port: secondPort, Source: config.NodeSourceInline,
	}); err != nil {
		t.Fatal(err)
	}
	if err := manager.TriggerReload(ctx); err != nil {
		t.Fatal(err)
	}
	manager.mu.RLock()
	afterAdd := manager.currentBox
	manager.mu.RUnlock()
	if afterAdd != initialBox {
		t.Fatal("adding one node replaced the sing-box instance")
	}
	assertListening(t, firstPort, true)
	assertListening(t, secondPort, true)

	if err := manager.DeleteNode(ctx, "runtime-b"); err != nil {
		t.Fatal(err)
	}
	if err := manager.TriggerReload(ctx); err != nil {
		t.Fatal(err)
	}
	manager.mu.RLock()
	afterDelete := manager.currentBox
	manager.mu.RUnlock()
	if afterDelete != initialBox {
		t.Fatal("deleting one node replaced the sing-box instance")
	}
	assertListening(t, firstPort, true)
	assertListening(t, secondPort, false)

	if err := manager.DeleteNode(ctx, "runtime-a"); err != nil {
		t.Fatal(err)
	}
	if err := manager.TriggerReload(ctx); err != nil {
		t.Fatal(err)
	}
	manager.mu.RLock()
	afterEmpty := manager.currentBox
	emptyRuntimeCfg := cloneConfig(manager.runtimeCfg)
	manager.mu.RUnlock()
	if afterEmpty != nil || emptyRuntimeCfg == nil || len(emptyRuntimeCfg.Nodes) != 0 {
		t.Fatalf("currentBox=%v runtimeCfg=%#v, want management-only runtime", afterEmpty, emptyRuntimeCfg)
	}
	assertListening(t, firstPort, false)

	if _, err := manager.CreateNode(ctx, config.NodeConfig{
		Name: "runtime-c", URI: "socks5://127.0.0.1:3", Port: secondPort, Source: config.NodeSourceInline,
	}); err != nil {
		t.Fatal(err)
	}
	if err := manager.TriggerReload(ctx); err != nil {
		t.Fatal(err)
	}
	manager.mu.RLock()
	afterRestart := manager.currentBox
	manager.mu.RUnlock()
	if afterRestart == nil {
		t.Fatal("adding a node did not restart the sing-box instance")
	}
	assertListening(t, firstPort, true)
}

func freePort(t *testing.T) uint16 {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	return uint16(listener.Addr().(*net.TCPAddr).Port)
}

func assertListening(t *testing.T, port uint16, want bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", fmt.Sprint(port)), 100*time.Millisecond)
		if err == nil {
			conn.Close()
		}
		if (err == nil) == want {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("port %d listening = %v, want %v", port, err == nil, want)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
