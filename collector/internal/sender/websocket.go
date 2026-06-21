package sender

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/game-coach/collector/internal/event"
	"github.com/game-coach/collector/internal/lol"
	"github.com/gorilla/websocket"
)

const (
	bufferCapacity = 200
	pingInterval   = 30 * time.Second
	pingTimeout    = 10 * time.Second
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

// WebSocket wraps a gorilla connection with reconnect, buffering, and heartbeat.
// writeMu protects all conn.WriteMessage calls; readMu protects SetReadDeadline.
type WebSocket struct {
	url     string
	conn    *websocket.Conn
	writeMu sync.Mutex // protects conn.WriteMessage
	readMu  sync.Mutex // protects conn.SetReadDeadline
	buffer  *ringBuffer
}

func NewWebSocket(url string) *WebSocket {
	return &WebSocket{url: url, buffer: &ringBuffer{}}
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
	conn, _, err := dialer.DialContext(ctx, w.url, nil)
	if err != nil {
		return fmt.Errorf("dial %s: %w", w.url, err)
	}

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
		for _, data := range replay {
			w.writeMu.Lock()
			err := conn.WriteMessage(websocket.TextMessage, data)
			w.writeMu.Unlock()
			if err != nil {
				log.Printf("replay write failed: %v (stopping replay)", err)
				break
			}
		}
	}

	return nil
}

func (w *WebSocket) readLoop(conn *websocket.Conn) {
	// Initial read deadline; heartbeat+pong handler will keep it refreshed.
	w.readMu.Lock()
	conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
	w.readMu.Unlock()

	conn.SetPongHandler(func(string) error {
		w.readMu.Lock()
		conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
		w.readMu.Unlock()
		return nil
	})

	for {
		_, msg, err := conn.ReadMessage()
		if err != nil {
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

func (w *WebSocket) heartbeat(conn *websocket.Conn) {
	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()

	for {
		w.readMu.Lock()
		conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
		w.readMu.Unlock()

		w.writeMu.Lock()
		err := conn.WriteMessage(websocket.PingMessage, nil)
		w.writeMu.Unlock()
		if err != nil {
			return
		}
		<-ticker.C
	}
}

func (w *WebSocket) Close() {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	if w.conn != nil {
		_ = w.conn.Close()
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

	err = w.conn.WriteMessage(websocket.TextMessage, body)
	if err != nil {
		// Connection is dead — nil it so concurrent senders (e.g. LCU poller)
		// will buffer their events instead of hitting the same dead connection.
		w.conn = nil
		w.buffer.push(body)
	}
	return err
}

func (w *WebSocket) SendState(state *lol.GameState) error {
	return w.send("state", state)
}

func (w *WebSocket) SendEvent(ev event.Event) error {
	return w.send("event", ev)
}
