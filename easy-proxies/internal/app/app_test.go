package app

import (
	"path/filepath"
	"testing"

	"easy_proxies/internal/config"
	"easy_proxies/internal/importer"
)

func TestSyncPoolRuntimePortsPersistsRebuiltAssignments(t *testing.T) {
	store, err := importer.NewStore(filepath.Join(t.TempDir(), "managed_nodes.json"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	pool := []importer.ManagedNode{{
		ID: "node", Name: "old", URI: "socks5://127.0.0.1:1", Port: 24000,
		State: importer.StateInPool, InPool: true,
	}}
	if err := store.UpsertNodes(pool); err != nil {
		t.Fatal(err)
	}
	configured := []config.NodeConfig{{Name: "runtime", URI: pool[0].URI, Port: 24005}}
	if err := syncPoolRuntimePorts(store, pool, configured); err != nil {
		t.Fatal(err)
	}
	updated, ok := store.GetNode("node")
	if !ok || updated.Port != 24005 || updated.Name != "runtime" {
		t.Fatalf("updated node = %#v, found = %v", updated, ok)
	}
}
