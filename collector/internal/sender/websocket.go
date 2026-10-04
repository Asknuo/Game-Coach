package sender

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"github.com/game-coach/collector/internal/event"
	"github.com/game-coach/collector/internal/lol"
	"github.com/gorilla/websocket"
)

const (
	bufferCapacity = 200
	pingInterval   = 30 * time.Second
	pingTimeout    = 10 * time.Second

	// writeTimeout bounds every WriteMessage. Without a write deadline a peer
	// that stops reading blocks the write forever, which hangs both the main
	// loop (send holds writeMu) and the LCU poller goroutine — SIGTERM then
	// cannot take the process down.
	writeTimeout = 5 * time.Second
)

type bufferedMsg struct {
	bytes   []byte
	stampMs int64
}

type ringBuffer struct {
	mu    sync.Mutex
	ring  [bufferCapacity]bufferedMsg
	head  int
	count int
}

func (b *ringBuffer) push(data []byte) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.ring[b.head] = bufferedMsg{bytes: data, stampMs: time.Now().UnixMilli()}
	b.head = (b.head + 1) % bufferCapacity
	if b.count < bufferCapacity {
		b.count++
	}
}

func (b *ringBuffer) drainSince(cutoffMs int64) [][]byte {
	b.mu.Lock()
	result := make([][]byte, 0, b.count)
	for i := 0; i < b.count; i++ {
		idx := (b.head - b.count + i + bufferCapacity) % bufferCapacity
		if b.ring[idx].stampMs >= cutoffMs {
			result = append(result, b.ring[idx].bytes)
		}
	}
	// Reset fields individually — never zero the mutex while holding it.
	b.head = 0
	b.count = 0
	b.ring = [bufferCapacity]bufferedMsg{}
	b.mu.Unlock()
	return result
}

// wsConn is one attempt at a connection: it guards the gorilla connection so a
// failed write poisons it for every other goroutine.
//
// Poisoning is required, not just tidy: when WriteMessage fails half-way through
// a frame, gorilla leaves the frame's writer open, and the next WriteMessage on
// the same conn panics ("concurrent write to websocket connection"). A panic in
// a goroutine kills the process, so after e.g. a write timeout in send(), the
// heartbeat's ping must not touch that conn again — it must fail cleanly and
// let the heartbeat exit.
type wsConn struct {
	conn *websocket.Conn
	dead atomic.Bool
	once sync.Once
}

func newWSConn(c *websocket.Conn) *wsConn { return &wsConn{conn: c} }

// write applies the write deadline, poisons on failure and reports the error.
func (p *wsConn) write(timeout time.Duration, msgType int, data []byte) error {
	if p.dead.Load() {
		return fmt.Errorf("connection already dead (write skipped)")
	}
	if timeout > 0 {
		_ = p.conn.SetWriteDeadline(time.Now().Add(timeout))
	}
	if err := p.conn.WriteMessage(msgType, data); err != nil {
		p.kill()
		return err
	}
	return nil
}

// kill closes the underlying connection exactly once and blocks later writes.
// Safe from any goroutine.
func (p *wsConn) kill() {
	p.dead.Store(true)
	p.once.Do(func() { _ = p.conn.Close() })
}

func (p *wsConn) isDead() bool { return p.dead.Load() }

// WebSocket wraps a gorilla connection with reconnect, buffering, and heartbeat.
// writeMu protects all conn.WriteMessage calls; readMu protects SetReadDeadline.
type WebSocket struct {
	url     string
	conn    *wsConn
	writeMu sync.Mutex // protects conn.WriteMessage
	readMu  sync.Mutex // protects conn.SetReadDeadline
	buffer  *ringBuffer

	// writeTimeout bounds each frame write; overwritten by tests.
	writeTimeout time.Duration
}

func NewWebSocket(url string) *WebSocket {
	return &WebSocket{url: url, buffer: &ringBuffer{}, writeTimeout: writeTimeout}
}

// writeMsg writes one frame under a bounded write deadline.
// A failed write means the connection is dead: it is closed and dropped from
// w.conn, so every later caller buffers instead of writing into a dead socket.
// Callers must hold writeMu — writeMsg never takes it itself.
func (w *WebSocket) writeMsg(conn *wsConn, msgType int, data []byte) error {
	if err := conn.write(w.writeTimeout, msgType, data); err != nil {
		w.dropConn(conn)
		return err
	}
	return nil
}

// dropConn closes a connection whose write just failed and clears w.conn when
// it still points at it. The identity check guarantees a newer connection is
// never closed or nil-ed by a goroutine that owns a stale one.
// Callers must hold writeMu.
func (w *WebSocket) dropConn(conn *wsConn) {
	conn.kill()
	if w.conn == conn {
		w.conn = nil
	}
}

// closeIfCurrent is dropConn for goroutines that do not hold writeMu
// (readLoop/heartbeat own their captured connection).
func (w *WebSocket) closeIfCurrent(conn *wsConn) {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	w.dropConn(conn)
}

func (w *WebSocket) Connect(ctx context.Context) error {
	// Quick check under writeMu — avoid double-dial without blocking I/O.
	w.writeMu.Lock()
	if w.conn != nil {
		w.writeMu.Unlock()
		return nil
	}
	w.writeMu.Unlock()

	dialer := websocket.Dialer{HandshakeTimeout: 5 * time.Second}
	raw, _, err := dialer.DialContext(ctx, w.url, nil)
	if err != nil {
		return fmt.Errorf("dial %s: %w", w.url, err)
	}
	conn := newWSConn(raw)

	w.writeMu.Lock()
	w.conn = conn
	w.writeMu.Unlock()

	go w.readLoop(conn)
	go w.heartbeat(conn)
	log.Printf("connected to agent at %s", w.url)

	// Replay buffered events from the last 60s.
	cutoff := time.Now().Add(-60 * time.Second).UnixMilli()
	replay := w.buffer.drainSince(cutoff)
	if len(replay) > 0 {
		log.Printf("replaying %d buffered events (last 60s)", len(replay))
		for i, data := range replay {
			// writeMu still guards every WriteMessage — including here, where
			// the heartbeat goroutine may already be pinging on this conn.
			w.writeMu.Lock()
			// writeMsg applies the write deadline and drops the connection on
			// failure, so a peer that stopped reading cannot hang the replay.
			err := w.writeMsg(conn, websocket.TextMessage, data)
			w.writeMu.Unlock()
			if err != nil {
				log.Printf("replay write failed: %v (stopping replay)", err)
				// Everything from the failing frame on was already drained from
				// the ring buffer — hand it back for the next reconnect.
				for _, rest := range replay[i:] {
					w.buffer.push(rest)
				}
				break
			}
		}
	}

	return nil
}

func (w *WebSocket) readLoop(conn *wsConn) {
	// Initial read deadline; heartbeat+pong handler will keep it refreshed.
	w.readMu.Lock()
	conn.conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
	w.readMu.Unlock()

	conn.conn.SetPongHandler(func(string) error {
		w.readMu.Lock()
		conn.conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
		w.readMu.Unlock()
		return nil
	})

	for {
		_, msg, err := conn.conn.ReadMessage()
		if err != nil {
			// The peer is gone: close our side and clear w.conn immediately.
			// Without this the half-open connection (and its FD) stayed alive
			// until the heartbeat or the read deadline noticed.
			w.closeIfCurrent(conn)
			return
		}
		var envelope struct {
			Type    string          `json:"type"`
			Payload json.RawMessage `json:"payload"`
		}
		if err := json.Unmarshal(msg, &envelope); err != nil {
			continue
		}
		if envelope.Type == "tip" {
			var tip struct {
				Message  string `json:"message"`
				Skill    string `json:"skill"`
				Priority int    `json:"priority"`
			}
			if err := json.Unmarshal(envelope.Payload, &tip); err == nil {
				log.Printf("[COACH] (%s) %s", tip.Skill, tip.Message)
			}
		}
	}
}

func (w *WebSocket) heartbeat(conn *wsConn) {
	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()

	for {
		w.readMu.Lock()
		conn.conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
		w.readMu.Unlock()

		w.writeMu.Lock()
		// Bounded write: a peer that stopped reading must not block the tick.
		err := w.writeMsg(conn, websocket.PingMessage, nil)
		w.writeMu.Unlock()
		if err != nil {
			// writeMsg already closed and dropped the connection.
			return
		}
		<-ticker.C
	}
}

func (w *WebSocket) Close() {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	if w.conn != nil {
		w.conn.kill()
		w.conn = nil
	}
}

func (w *WebSocket) send(msgType string, payload interface{}) error {
	body, err := json.Marshal(map[string]interface{}{
		"type":    msgType,
		"payload": payload,
	})
	if err != nil {
		return err
	}

	w.writeMu.Lock()
	defer w.writeMu.Unlock()

	if w.conn == nil {
		w.buffer.push(body)
		return fmt.Errorf("not connected (buffered)")
	}

	if err := w.writeMsg(w.conn, websocket.TextMessage, body); err != nil {
		// Connection is dead — writeMsg closed it and nil-ed w.conn, so
		// concurrent senders (e.g. the LCU poller) now buffer instead of hitting
		// the same dead connection. Keep this payload for reconnect replay.
		w.buffer.push(body)
		return err
	}
	return nil
}

func (w *WebSocket) SendState(state *lol.GameState) error {
	return w.send("state", state)
}

func (w *WebSocket) SendEvent(ev event.Event) error {
	return w.send("event", ev)
}
