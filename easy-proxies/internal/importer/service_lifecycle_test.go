package importer

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

func TestCancelBatchPreservesConcurrentImport(t *testing.T) {
	svc, store := newBatchServiceForTest(t, &batchNodeManagerStub{})
	tester := NewNodeTester(nil, WithTesterConcurrency(1))
	defer tester.Close()
	svc.tester = tester
	started := make(chan struct{})
	var once sync.Once
	tester.probeOverride = func(ctx context.Context, _ ManagedNode, _ string, _ time.Duration) TestResult {
		once.Do(func() { close(started) })
		<-ctx.Done()
		return TestResult{Error: ctx.Err()}
	}
	if err := store.UpsertNode(ManagedNode{ID: "tested", URI: "socks5://127.0.0.1:1", State: StateFailed}); err != nil {
		t.Fatal(err)
	}
	jobID, err := svc.StartBatchTest(BatchTestRequest{NodeIDs: []string{"tested"}, Retest: true})
	if err != nil {
		t.Fatal(err)
	}
	<-started
	if err := store.UpsertNode(ManagedNode{ID: "new-import", URI: "socks5://127.0.0.1:2", State: StateFailed}); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.CancelTestJob(jobID); err != nil {
		t.Fatal(err)
	}
	job := waitTestJobTerminal(t, svc, jobID)
	if job.Status != TestJobCanceled {
		t.Fatalf("job status = %s, want canceled", job.Status)
	}
	if _, ok := store.GetNode("new-import"); !ok {
		t.Fatal("canceling a batch discarded a node imported while probes were running")
	}
}

func TestBatchAdmissionRejectsDuplicatesAndSurvivesInvalidPolicy(t *testing.T) {
	svc, store := newBatchServiceForTest(t, &batchNodeManagerStub{})
	if err := store.UpsertNode(ManagedNode{ID: "node", URI: "socks5://127.0.0.1:1", State: StateFailed}); err != nil {
		t.Fatal(err)
	}
	req := BatchTestRequest{NodeIDs: []string{"node"}, Retest: true, SiteTargets: []string{"invalid"}}
	if _, err := svc.StartBatchTest(req); err == nil {
		t.Fatal("invalid policy accepted")
	}
	if !svc.testStartMu.TryLock() {
		t.Fatal("invalid policy leaked the admission lock")
	}
	svc.testStartMu.Unlock()
	svc.testJobsMu.Lock()
	svc.testJobs["active"] = &TestJob{ID: "active", Status: TestJobRunning}
	svc.testJobsMu.Unlock()
	req.SiteTargets = nil
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if _, err := svc.StartBatchTest(req); !errors.Is(err, ErrBatchTestBusy) {
				t.Errorf("duplicate batch error=%v", err)
			}
		}()
	}
	wg.Wait()
}

func TestBatchAndRefreshAdmissionAreMutuallyExclusive(t *testing.T) {
	svc, store := newBatchServiceForTest(t, &batchNodeManagerStub{})
	if err := store.UpsertNode(ManagedNode{ID: "node", URI: "socks5://127.0.0.1:1", TagPrefix: "local", ImportMode: "content", State: StateFailed}); err != nil {
		t.Fatal(err)
	}
	svc.testJobsMu.Lock()
	svc.testJobs["test"] = &TestJob{ID: "test", Status: TestJobRunning}
	svc.testJobsMu.Unlock()
	if _, err := svc.StartRefreshSources(""); !errors.Is(err, ErrBatchTestBusy) {
		t.Fatalf("refresh admission error = %v, want ErrBatchTestBusy", err)
	}
	svc.testJobsMu.Lock()
	delete(svc.testJobs, "test")
	svc.testJobsMu.Unlock()
	svc.refreshJobsMu.Lock()
	svc.refreshJobs["refresh"] = &SourceRefreshJob{ID: "refresh", Status: SourceRefreshJobRunning}
	svc.refreshJobsMu.Unlock()
	if _, err := svc.StartBatchTest(BatchTestRequest{NodeIDs: []string{"node"}, Retest: true}); !errors.Is(err, ErrBatchTestBusy) {
		t.Fatalf("batch admission error = %v, want ErrBatchTestBusy", err)
	}
	if id, err := svc.StartRefreshSources(""); err != nil || id != "refresh" {
		t.Fatalf("duplicate refresh = %q, %v; want the existing job", id, err)
	}
}

func TestServiceCloseCancelsAndWaitsForBackgroundJobs(t *testing.T) {
	service := &Service{
		importCancels:  make(map[string]context.CancelFunc),
		testCancels:    make(map[string]context.CancelFunc),
		refreshCancels: make(map[string]context.CancelFunc),
	}
	started := make(chan struct{})
	finished := make(chan struct{})
	var once sync.Once
	if !service.launchBackground(func(cancel context.CancelFunc) {
		service.importCancelsMu.Lock()
		service.importCancels["job"] = cancel
		service.importCancelsMu.Unlock()
	}, func(ctx context.Context) {
		close(started)
		<-ctx.Done()
		once.Do(func() { close(finished) })
	}) {
		t.Fatal("background job did not start")
	}
	<-started
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := service.Close(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-finished:
	default:
		t.Fatal("Close returned before the job stopped")
	}
	if service.launchBackground(func(context.CancelFunc) {}, func(context.Context) {}) {
		t.Fatal("service accepted a job after Close")
	}
}
