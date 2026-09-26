package subscription

import (
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"easy_proxies/internal/config"
	"easy_proxies/internal/importer"
)

type sequencedSourceRefresher struct {
	fakeSourceRefresher
	reads   atomic.Int32
	entered [2]chan struct{}
	release [2]chan struct{}
}

func (f *sequencedSourceRefresher) GetRefreshJob(string) (importer.SourceRefreshJob, bool) {
	i := f.reads.Add(1) - 1
	close(f.entered[i])
	<-f.release[i]
	return f.job, true
}

func TestUpdateWaitsForNewRefreshInsteadOfPreviousCompletion(t *testing.T) {
	cfg := &config.Config{
		Subscriptions:       []string{"https://example.test/old"},
		SubscriptionRefresh: config.SubscriptionRefreshConfig{Enabled: true, Interval: time.Hour, Timeout: 3 * time.Second},
	}
	cfg.SetFilePath(filepath.Join(t.TempDir(), "config.yaml"))
	if err := cfg.SaveFull(); err != nil {
		t.Fatal(err)
	}
	mgr := New(cfg, nil)
	defer mgr.Stop()
	refresher := &sequencedSourceRefresher{
		fakeSourceRefresher: fakeSourceRefresher{job: importer.SourceRefreshJob{ID: "job", Status: importer.SourceRefreshJobFinished}},
		entered:             [2]chan struct{}{make(chan struct{}), make(chan struct{})},
		release:             [2]chan struct{}{make(chan struct{}), make(chan struct{})},
	}
	mgr.SetSourceRefresher(refresher)
	mgr.Start()
	mgr.manualRefresh <- struct{}{}
	<-refresher.entered[0]
	oldCtx := mgr.ctx
	done := make(chan error, 1)
	go func() { done <- mgr.UpdateConfigAndRefresh([]string{"https://example.test/new"}, false, time.Hour) }()
	<-oldCtx.Done()
	close(refresher.release[0])
	<-refresher.entered[1]
	returnedEarly := false
	select {
	case <-done:
		returnedEarly = true
	case <-time.After(650 * time.Millisecond):
	}
	close(refresher.release[1])
	if returnedEarly {
		t.Fatal("config update reported completion while its new refresh was still running")
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("config update did not observe the new refresh completing")
	}
}

type blockingSourceRefresher struct {
	fakeSourceRefresher
	entered chan struct{}
	release chan struct{}
}

func (f *blockingSourceRefresher) GetRefreshJob(string) (importer.SourceRefreshJob, bool) {
	close(f.entered)
	<-f.release
	return f.job, true
}

func TestLifecycleWaitsForPreviousRefresh(t *testing.T) {
	for _, reconfigure := range []bool{false, true} {
		name := "stop"
		if reconfigure {
			name = "reconfigure"
		}
		t.Run(name, func(t *testing.T) {
			mgr := New(&config.Config{
				Subscriptions:       []string{"https://example.test/sub"},
				SubscriptionRefresh: config.SubscriptionRefreshConfig{Enabled: true, Interval: time.Hour},
			}, nil)
			defer mgr.Stop()
			refresher := &blockingSourceRefresher{
				fakeSourceRefresher: fakeSourceRefresher{job: importer.SourceRefreshJob{ID: "job", Status: importer.SourceRefreshJobFinished}},
				entered:             make(chan struct{}), release: make(chan struct{}),
			}
			mgr.SetSourceRefresher(refresher)
			oldCtx := mgr.ctx
			mgr.Start()
			mgr.manualRefresh <- struct{}{}
			<-refresher.entered
			done := make(chan struct{})
			go func() {
				if reconfigure {
					mgr.ApplyRestoredConfig(&config.Config{})
				} else {
					mgr.Stop()
				}
				close(done)
			}()
			<-oldCtx.Done()
			returnedEarly := false
			select {
			case <-done:
				returnedEarly = true
			case <-time.After(30 * time.Millisecond):
			}
			close(refresher.release)
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("lifecycle operation did not finish after the refresh returned")
			}
			if returnedEarly {
				t.Fatal("lifecycle operation returned while the previous refresh was still running")
			}
		})
	}
}

func TestConfigUpdateCannotRestartStoppedManager(t *testing.T) {
	cfg := &config.Config{SubscriptionRefresh: config.SubscriptionRefreshConfig{Interval: time.Hour}}
	cfg.SetFilePath(filepath.Join(t.TempDir(), "config.yaml"))
	mgr := New(cfg, nil)
	refresher := &fakeSourceRefresher{job: importer.SourceRefreshJob{ID: "job", Status: importer.SourceRefreshJobFinished}}
	mgr.SetSourceRefresher(refresher)
	mgr.Stop()
	defer mgr.Stop()
	mgr.UpdateConfig([]string{"https://example.test/sub"}, false, time.Hour)
	time.Sleep(50 * time.Millisecond)
	if refresher.starts.Load() != 0 {
		t.Fatal("config update restarted a stopped subscription manager")
	}
}

func TestStartIsIdempotentAndReconfigureReplacesLoop(t *testing.T) {
	cfg := &config.Config{
		Subscriptions:       []string{"https://example.test/sub"},
		SubscriptionRefresh: config.SubscriptionRefreshConfig{Enabled: true, Interval: time.Hour},
	}
	mgr := New(cfg, nil)
	defer mgr.Stop()
	mgr.Start()
	first := mgr.loopDone
	mgr.Start()
	if mgr.loopDone != first {
		t.Fatal("Start created a second refresh loop")
	}
	mgr.ApplyRestoredConfig(&config.Config{Subscriptions: cfg.Subscriptions})
	select {
	case <-first:
	default:
		t.Fatal("previous loop is still running after reconfiguration")
	}
	last := mgr.loopDone
	if last == nil || last == first {
		t.Fatal("reconfiguration did not create a new loop")
	}
	mgr.Stop()
	select {
	case <-last:
	default:
		t.Fatal("refresh loop is still running after Stop")
	}
}
