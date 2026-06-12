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

type WebSocket struct {
	url  string
	conn *websocket.Conn
	mu   sync.Mutex
}

func NewWebSocket(url string) *WebSocket {
	return &WebSocket{url: url}
}

func (w *WebSocket) Connect(ctx context.Context) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w.conn != nil {
		return nil
	}

	dialer := websocket.Dialer{HandshakeTimeout: 5 * time.Second}
	conn, _, err := dialer.DialContext(ctx, w.url, nil)
	if err != nil {
		return fmt.Errorf("dial %s: %w", w.url, err)
	}

	w.conn = conn
	go w.readLoop(conn)
	log.Printf("connected to agent at %s", w.url)
	return nil
}

func (w *WebSocket) readLoop(conn *websocket.Conn) {
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

func (w *WebSocket) Close() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.conn != nil {
		_ = w.conn.Close()
		w.conn = nil
	}
}

func (w *WebSocket) send(msgType string, payload interface{}) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w.conn == nil {
		return fmt.Errorf("not connected")
	}

	body, err := json.Marshal(map[string]interface{}{
		"type":    msgType,
		"payload": payload,
	})
	if err != nil {
		return err
	}

	return w.conn.WriteMessage(websocket.TextMessage, body)
}

func (w *WebSocket) SendState(state *lol.GameState) error {
	return w.send("state", state)
}

func (w *WebSocket) SendEvent(ev event.Event) error {
	return w.send("event", ev)
}
