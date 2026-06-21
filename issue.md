# LCU WebSocket "close sent" 错误

## 现象

collector 日志中反复出现 `[LCU] send event failed: websocket: close sent` 错误：

```
2026/06/21 17:23:45 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:02 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:02 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:02 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:02 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:30 [LCU] send event failed: websocket: close sent
2026/06/21 17:24:40 [LCU] send event failed: websocket: close sent
```

## 根因

[collector/cmd/main.go](collector/cmd/main.go#L62) 中 LCU poller 作为独立 goroutine 运行：

```go
go lcuPoller.Run(ctx)
```

其回调中调用 `ws.SendEvent()` 通过 WebSocket 发送事件。主循环（`runLoop`）与 LCU poller 共享同一个 `ws` 连接。

当 WebSocket 断开时：
1. `runLoop` 中 send 失败，返回 error
2. 在主循环调用 `ws.Close()` 清零 `w.conn` 之前，LCU poller 回调仍在并发调用 `ws.SendEvent()`
3. `send()` 发现 `w.conn` 非 nil（尚未被清零），调用 `conn.WriteMessage()` 写入已死连接
4. gorilla/websocket 返回 `ErrCloseSent`

## 修复

[collector/internal/sender/websocket.go](collector/internal/sender/websocket.go#L200-L206) — 在 `send()` 中，当 `WriteMessage` 失败时立即将 `w.conn = nil`：

```go
err = w.conn.WriteMessage(websocket.TextMessage, body)
if err != nil {
    w.conn = nil  // 立即清零，后续并发 send 走缓冲路径
    w.buffer.push(body)
}
```

此后其他 goroutine 调用 `send()` 会命中 nil 检查，将事件缓冲而非尝试写入已死连接。事件数据不会丢失 — 缓冲事件在重连后会被重放。

所有 `w.conn` 的访问均受 `writeMu` 互斥锁保护，无竞态条件。

---

# GameState `all_players` 为 null 导致 Pydantic 校验失败

## 现象

agent 日志中反复报错：

```
ERROR:agent:websocket error
pydantic_core._pydantic_core.ValidationError: 1 validation error for GameState
all_players
  Input should be a valid list [type=list_type, input_value=None, input_type=NoneType]
```

## 根因

[collector/internal/lol/parser.go](collector/internal/lol/parser.go#L81-L83) 中 `ParseGameState` 创建 `GameState` 时，`AllPlayers`（Go 切片）默认值为 nil。

当 Live Client API 的 `/liveclientdata/allgamedata` 响应中缺少 `allPlayers` 字段时（出现在加载画面、游戏刚开局等阶段），切片保持 nil。Go 的 `json.Marshal` 将 nil 切片序列化为 `null`。

Python 端 [agent/models/state.py](agent/models/state.py#L67) 的 `GameState.all_players` 声明为 `list[Player]`，Pydantic 拒绝 `null` 值。

## 修复

[collector/internal/lol/parser.go](collector/internal/lol/parser.go) — `ParseGameState` 初始化时将 `AllPlayers` 和 `Events` 预分配为空切片：

```go
state := &GameState{
    CollectedAt: time.Now().UTC(),
    AllPlayers:  []Player{},    // 确保 JSON 输出为 [] 而非 null
    Events:      []GameEvent{},
}
```

空切片在 Go 中序列化为 `[]`，Pydantic 可以正常处理。
