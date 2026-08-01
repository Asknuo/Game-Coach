package sender

import (
	"testing"
	"time"
)

func TestRingBuffer_Empty(t *testing.T) {
	b := &ringBuffer{}
	if got := b.drainSince(0); len(got) != 0 {
		t.Fatalf("expected empty drain, got %d items", len(got))
	}
}

func TestRingBuffer_CapacityAndOrder(t *testing.T) {
	b := &ringBuffer{}
	for i := 0; i < bufferCapacity+10; i++ {
		b.push([]byte{byte(i)})
	}
	got := b.drainSince(0)
	if len(got) != bufferCapacity {
		t.Fatalf("drained %d items, want %d", len(got), bufferCapacity)
	}
	if got[0][0] != byte(10) {
		t.Errorf("oldest item = %d, want 10", got[0][0])
	}
	if got[len(got)-1][0] != byte(bufferCapacity+9) {
		t.Errorf("newest item = %d, want %d", got[len(got)-1][0], bufferCapacity+9)
	}
	if got := b.drainSince(0); len(got) != 0 {
		t.Fatalf("second drain must be empty, got %d items", len(got))
	}
}

func TestRingBuffer_DrainSinceCutoff(t *testing.T) {
	b := &ringBuffer{}
	b.push([]byte("old"))
	cutoff := nextMillis(t)
	b.push([]byte("new"))

	got := b.drainSince(cutoff)
	if len(got) != 1 || string(got[0]) != "new" {
		t.Fatalf("drainSince cutoff = %q, want [new]", got)
	}
}

func nextMillis(t *testing.T) int64 {
	t.Helper()
	now := time.Now().UnixMilli()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().UnixMilli() == now && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if time.Now().UnixMilli() == now {
		t.Fatal("clock did not advance within deadline")
	}
	return time.Now().UnixMilli()
}
