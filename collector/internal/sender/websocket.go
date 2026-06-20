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

// ── 常量 ────────────────────────────────────────────

const (
	bufferCapacity = 200                  // HA #3: 环形缓冲区容量
	pingInterval   = 30 * time.Second     // HA #4: 心跳间隔
	pingTimeout    = 10 * time.Second     // HA #4: 等待 pong 的超时
)

type bufferedMsg struct {
	bytes   []byte
	stampMs int64 // UnixMilli，用于过滤过期消息
}

// ringBuffer 固定大小环形缓冲区，线程安全.
type ringBuffer struct {
	mu    sync.Mutex
	ring  [bufferCapacity]bufferedMsg
	head  int // 写入位置
	count int // 当前已用条目数
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

// drainSince 返回时间戳 >= cutoffMs 的所有消息（按时间顺序），并清空缓冲区.
func (b *ringBuffer) drainSince(cutoffMs int64) [][]byte {
	b.mu.Lock()
	defer b.mu.Unlock()
	result := make([][]byte, 0, b.count)
	for i := 0; i < b.count; i++ {
		idx := (b.head - b.count + i + bufferCapacity) % bufferCapacity
		if b.ring[idx].stampMs >= cutoffMs {
			result = append(result, b.ring[idx].bytes)
		}
	}
	// 清空
	*b = ringBuffer{}
	return result
}

// ── WebSocket ────────────────────────────────────────

type WebSocket struct {
	url    string
	conn   *websocket.Conn
	mu     sync.Mutex
	buffer *ringBuffer // ★ HA #3: 断连时缓冲消息
}

func NewWebSocket(url string) *WebSocket {
	return &WebSocket{url: url, buffer: &ringBuffer{}}
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
	go w.heartbeat(conn) // ★ HA #4: 30s 心跳协程
	log.Printf("connected to agent at %s", w.url)

	// ★ HA #3: 重连后重放最近 60 秒的缓冲事件
	cutoff := time.Now().Add(-60 * time.Second).UnixMilli()
	replay := w.buffer.drainSince(cutoff)
	if len(replay) > 0 {
		log.Printf("replaying %d buffered events (last 60s)", len(replay))
		for _, data := range replay {
			if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
				log.Printf("replay write failed: %v (stopping replay)", err)
				break
			}
		}
	}

	return nil
}

func (w *WebSocket) readLoop(conn *websocket.Conn) {
	// ★ HA #4: 设置 pong 处理器，每次收到 pong 刷新读超时
	conn.SetPongHandler(func(string) error {
		conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
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

// ★ HA #4: 心跳协程 — 每 30s 发 ping，pong 超时关闭连接
func (w *WebSocket) heartbeat(conn *websocket.Conn) {
	ticker := time.NewTicker(pingInterval)
	defer ticker.Stop()

	for {
		conn.SetReadDeadline(time.Now().Add(pingInterval + pingTimeout))
		err := conn.WriteMessage(websocket.PingMessage, nil)
		if err != nil {
			return // 连接已断，协程退出
		}
		<-ticker.C
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
	body, err := json.Marshal(map[string]interface{}{
		"type":    msgType,
		"payload": payload,
	})
	if err != nil {
		return err
	}

	w.mu.Lock()
	defer w.mu.Unlock()

	if w.conn == nil {
		// ★ HA #3: 未连接时缓冲消息
		w.buffer.push(body)
		return fmt.Errorf("not connected (buffered)")
	}

	err = w.conn.WriteMessage(websocket.TextMessage, body)
	if err != nil {
		// ★ HA #3: 发送失败也缓冲（下次重连回放）
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
