package sender

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// peerServer upgrades WebSocket connections and hands the raw TCP connection to
// the returned channel. silence keeps the handler (and therefore the peer)
// from ever reading.
func peerServer(t *testing.T, silence bool) (*httptest.Server, <-chan net.Conn) {
	t.Helper()
	upgrader := websocket.Upgrader{}
	peers := make(chan net.Conn, 4)
	hold := make(chan struct{})

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			return
		}
		peers <- c.UnderlyingConn()
		if silence {
			// Never read: the client's writes must hit the write deadline.
			<-hold
			return
		}
		for {
			if _, _, err := c.ReadMessage(); err != nil {
				return
			}
		}
	}))
	t.Cleanup(func() {
		close(hold)
		srv.Close()
	})
	return srv, peers
}

func wsURL(t *testing.T, srv *httptest.Server) string {
	t.Helper()
	return "ws" + strings.TrimPrefix(srv.URL, "http")
}

// Regression: without a write deadline a peer that stops reading blocks
// WriteMessage forever — send() holds writeMu, so the main loop and the LCU
// poller goroutine both hang and SIGTERM cannot take the process down.
func TestSend_WriteTimeoutDropsAndBuffers(t *testing.T) {
	srv, peers := peerServer(t, true)

	w := NewWebSocket(wsURL(t, srv))
	w.writeTimeout = 200 * time.Millisecond
	if err := w.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	peer := <-peers

	// Overflow the socket buffers so the next write blocks on the deadline.
	blob := strings.Repeat("x", 256*1024)
	var blocks int
	for i := 0; i < 16; i++ {
		payload := map[string]string{"blob": blob}
		if err := w.send("state", payload); err != nil {
			break
		}
		blocks++
	}
	if blocks == 16 {
		t.Skip("socket buffers absorbed every frame; cannot exercise the write deadline here")
	}

	if err := w.send("state", map[string]string{"after": "deadline"}); err == nil {
		t.Fatal("send after a write timeout must fail, not block forever")
	}
	if w.conn != nil {
		t.Fatal("dead connection must be dropped from w.conn")
	}
	w.writeMu.Lock()
	nilConn := w.conn == nil
	w.writeMu.Unlock()
	if !nilConn {
		t.Fatal("w.conn must be nil after a failed write")
	}
	if n := bufferedCount(w.buffer); n == 0 {
		t.Fatal("payload written after the timeout must be buffered for replay")
	}

	// The peer must observe the close — otherwise the socket and its goroutines
	// stay alive until the process exits.
	waitPeerClosed(t, peer)
}

// waitPeerClosed drains the peer socket until it sees EOF/reset. The deadline
// distinguishes "closed" from "still open" (a timeout means no close arrived).
func waitPeerClosed(t *testing.T, peer net.Conn) {
	t.Helper()
	const patience = 3 * time.Second
	start := time.Now()
	peer.SetReadDeadline(start.Add(patience))
	buf := make([]byte, 32*1024)
	for {
		_, err := peer.Read(buf)
		if err != nil {
			if time.Since(start) >= patience {
				t.Fatalf("peer read timed out — the client never closed the dead connection")
			}
			return
		}
	}
}

// The write timeout must reject quickly (bounded), not hang until the OS does.
func TestSend_WriteDeadlineIsBounded(t *testing.T) {
	srv, peers := peerServer(t, true)
	w := NewWebSocket(wsURL(t, srv))
	w.writeTimeout = 150 * time.Millisecond
	if err := w.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	<-peers

	blob := strings.Repeat("y", 256*1024)
	start := time.Now()
	deadline := start.Add(5 * time.Second)
	for i := 0; i < 32 && time.Now().Before(deadline); i++ {
		if err := w.send("state", map[string]string{"blob": blob}); err != nil {
			if elapsed := time.Since(start); elapsed > 3*time.Second {
				t.Fatalf("write blocked for %v — deadline not applied", elapsed)
			}
			return
		}
	}
	t.Skip("socket buffers absorbed every frame; cannot exercise the write deadline here")
}

// readLoop must drop the connection when the peer disappears, so the next send
// buffers instead of writing into a half-open socket.
func TestReadLoop_PeerCloseDropsConn(t *testing.T) {
	srv, peers := peerServer(t, false)

	w := NewWebSocket(wsURL(t, srv))
	if err := w.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}
	peer := <-peers
	w.writeMu.Lock()
	alive := w.conn
	w.writeMu.Unlock()
	if alive == nil {
		t.Fatal("expected a live connection after Connect")
	}
	// The agent disappears: readLoop must notice and drop the connection.
	peer.Close()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		w.writeMu.Lock()
		conn := w.conn
		w.writeMu.Unlock()
		if conn == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("w.conn must be nil after the peer closed the connection")
}

// Regression: gorilla leaves a half-written frame behind when a write fails
// mid-frame, so the next WriteMessage on that conn panics — a panic from the
// heartbeat goroutine would take the whole process down. A failed write must
// poison the connection for every later writer.
func TestSend_PoisonedConnRejectsLaterWrites(t *testing.T) {
	srv, _ := peerServer(t, true)
	w := NewWebSocket(wsURL(t, srv))
	w.writeTimeout = 150 * time.Millisecond
	if err := w.Connect(context.Background()); err != nil {
		t.Fatalf("Connect: %v", err)
	}

	w.writeMu.Lock()
	handle := w.conn
	w.writeMu.Unlock()
	if handle == nil {
		t.Fatal("expected a live connection handle after Connect")
	}

	// Overflow the socket buffers so a write times out mid-frame.
	blob := strings.Repeat("x", 256*1024)
	failed := false
	for i := 0; i < 32; i++ {
		if err := w.send("state", map[string]string{"blob": blob}); err != nil {
			failed = true
			break
		}
	}
	if !failed {
		t.Skip("socket buffers absorbed every frame; cannot exercise the write deadline here")
	}
	if !handle.isDead() {
		t.Fatal("a failed write must poison the connection for other writers")
	}
	// The heartbeat may still hold this handle for up to pingInterval.
	if err := handle.write(w.writeTimeout, websocket.PingMessage, nil); err == nil {
		t.Fatal("a write to a poisoned connection must fail instead of panicking")
	}
}

func bufferedCount(b *ringBuffer) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.count
}

func TestRingBuffer_StillOrders(t *testing.T) {
	b := &ringBuffer{}
	b.push([]byte("a"))
	b.push([]byte("b"))
	got := b.drainSince(0)
	if len(got) != 2 || string(got[0]) != "a" || string(got[1]) != "b" {
		t.Fatalf("drain = %q, want [a b]", got)
	}
}

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
