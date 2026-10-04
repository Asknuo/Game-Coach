# BUGFIX — 代码审查发现问题的修复档案

日期：2026-10-04
范围：`agent/`（Python）、`collector/`（Go）、`voice/`（Python）、CI 与仓库卫生
背景：对三个组件做了一轮深度审查（正确性 / 并发 / 资源 / 错误处理 / 测试覆盖），
本文档记录每一个被修复的问题：位置、根因、修法、回归测试与验证方式。

**验证结果**：agent 80 + voice 26 + Go 73 = **179 个测试全绿**；`ruff check agent voice`
干净；`gofmt -l .` 空、`go vet ./...` 无警告、`go test ./...` 与 `go test -race ./...`
均通过；collector 可交叉编译 windows/linux/darwin。冒烟测试确认 app 装配
（7 skills / 9 节点 / 流式广播器接线）正常。

---

## P0 — 功能失效 / 脏数据

### B1. LOL 客户端重启后采集器永久 401（collector）

- **位置**：`collector/internal/lol/client.go:318`
- **根因**：lockfile 密码一次性缓存，`hasCreds` 只在刷新失败时置回 false；
  LOL 客户端重启会重写 lockfile 密码 → 端口 2999 全部 401 → 既不算 not-in-game
  也不触发刷新，只能重启 collector 进程。
- **修复**：`get()` 识别 401/403 → `invalidateCredentials()`（client.go:100）清空
  password 并把 `hasCreds.Store(false)`，下一 tick 自然重刷。密码改由 `mu` 保护。
  404 保留凭据（不触发无谓重扫）。
- **回归**：`TestGet_UnauthorizedInvalidatesCredentials`（401→丢凭据→lockfile 改写
  →下一帧用新 token 成功）/ `TestGet_ForbiddenInvalidatesCredentials` /
  `TestGet_NotFoundKeepsCredentials`

### B2. WebSocket 无写超时 → 进程级挂死（collector）

- **位置**：`collector/internal/sender/websocket.go`（三处写：重连重放 / 心跳 ping / send）
- **根因**：三处 `WriteMessage` 均无 `SetWriteDeadline`，且 send 全程持 `writeMu`。
  对端停止读取（agent 事件循环被同步 IO 卡住 / TCP 窗口满 / 半开连接）时写无限
  阻塞 → 主采集循环与 LCU poller 双 goroutine 卡死，SIGTERM 无法退出。
- **修复**：统一走 `writeMsg`（websocket.go:91 设 5s 写超时）；写失败即判"连接已死"：
  `dropConn` 关连接 + 置 nil + 消息入 ring buffer + 返回 error，与既有重连路径闭环；
  重放失败时未发出的剩余帧还回 ring buffer（原来会丢）。
- **额外加固**：gorilla 写失败半帧后再写会 `panic("concurrent write")` 杀进程
  （超时后 30s 内心跳还会 ping 同一 conn）——新增 `wsConn`（atomic + sync.Once）
  把死连接"下毒"，后续写返回 error 干净退出。
- **回归**：`TestSend_WriteTimeoutDropsAndBuffers` /
  `TestSend_WriteDeadlineIsBounded`（临时回滚验证：去掉 deadline 测试挂死 25s）/
  `TestSend_PoisonedConnRejectsLaterWrites` / `TestReadLoop_PeerCloseDropsConn`

### B3. 每局只有第一条龙/大龙提醒（collector）

- **位置**：`collector/internal/event/detector_objectives.go:13-40`
- **根因**：`dragonWarned/baronWarned` 布尔锁存，游戏内无清除路径（只有
  `engine.Reset()` 局间清零）。25 分钟的局有 2-4 条龙，第 2 条起全部被吞；
  agent 侧 routing/validation 也按"多次"设计。
- **修复**：改用 spawn 身份（`SpawnTime`）：`SecondsLeft<=30 && SpawnTime !=
  lastDragonSpawn` 才触发并记录；`timer==nil` 时清回哨兵 `noSpawn(-1)`
  （0 不能当哨兵，真实 spawn 时间 > 0）。`TestDetect_DragonSoonOnce` 的
  "只有一次"错误语义已改写。
- **回归**：`TestDetect_DragonSoonPerSpawn` / `TestDetect_BaronSoonPerSpawn` /
  `TestDetect_ObjectiveSoonRespectsWindow` / `TestDetect_ResetClearsSpawnLatch` /
  `TestDetect_DragonSoonAcrossRealSpawns`（驱动真实 ObjectiveTracker：
  同 spawn 20/10/5s 不重发，第 2、3 条龙必发）

### B4. 一次网络抖动 = 一条假对局记录 + 60s LLM 复盘（agent）

- **位置**：`agent/services/session.py:39-89`
- **根因**：任何 `WebSocketDisconnect` 且 `game_time>120` 就跑
  `summarize_on_disconnect`（review 流水线 30s + 摘要 30s 两个 LLM 预算），
  并往 `recent_games` 永久追加 `result:"unknown"` 的记录。而 collector 发送
  失败会主动 close WS（main.go:71），2 秒后重连成新会话 → 抖动即脏数据。
- **修复**：断连分级。`_saw_game_end`（收到 LCU `game_end` 事件时置位，
  session.py:39/167）为 True 才走复盘；否则只计 `session_aborts` 指标 +
  轻量清理，不写记录不跑 LLM。同时 `_review_published` 去重：game_end 已走过
  review 流水线产出 tip 时，断连复盘不再跑第二次 LLM（lifecycle.py:159
  新增 `skip_review` 参数）。
- **回归**：`test_transient_disconnect_skips_review_and_record` /
  `test_game_end_marked_from_event`

### B5. 被拒建议照样上屏，且流式卡片无关联 id（agent）

- **位置**：`agent/graph/nodes/generation.py:76-96`、`validation.py:46-61`、
  `context.py:126-140`、`services/broadcast.py:40`
- **根因**：`tip_stream` 在 `llm_polish` 内就开始推送，而 validate（low_confidence/
  duplicate）与 publish（hp_recovered/objective_gone/items_gone）完全可能拒发——
  overlay 已渲染的卡片没有权威 `tip` 落锤，也没有撤回机制；并发上限 4 条流
  （queue 2 + urgent 2）交错推送同结构 payload，无任何关联标识，客户端文本错盖。
- **修复**：
  1. 每条流水线生成 `tip_id`（`session:event:seq`，generation.py:35），
     流式增量、权威 `tip`、撤回消息三者共用（CoachState 新增 `tip_id` 字段）；
  2. validate/publish 的每个拒发分支调用 `_cancel_stream`（validation.py:46）
     经 `on_tip_cancel` 回调广播 `tip_cancel {tip_id, skill}`；
  3. `tip` payload 带 `tip_id`；协议文档同步更新（README 消息类型表 + 流式协议说明）。
  4. 流中断时保留已产出部分文本——overlay 已显示的内容必须与最终落锤一致
     （原来中断会回退到观众没见过的草稿）。
- **回归**：`test_graph_cancels_stream_when_muted`（图级端到端）/
  `test_reject_emits_tip_cancel` / `test_stream_mid_failure_keeps_partial_text` /
  voice 侧 `test_listen_ignores_tip_stream_and_malformed`

### B6. death 事件被双重死亡过滤吞掉，survival 的 death 分支是死代码（agent）

- **位置**：`agent/services/events.py:53`、`agent/graph/nodes/parsing.py:68`
- **根因**：`should_skip_dead_event`（session 层，先于 urgent 判断执行）与
  `detect_signals`（图内）都对 hp==0 的非龙事件返回跳过；而 `is_urgent` 把
  death 列为紧急、survival SKILL.md 声明监听 death——三个模块自相矛盾，
  该建议一次都不会发出，旧测试还把错误行为固化了。
- **修复**：`common.py` 新增 `DEAD_PASSTHROUGH_EVENTS = (dragon_soon,
  baron_soon, death)`，两处过滤统一用它；death 穿透后走 urgent 直启路径，
  survival 的复活期建议真正生效。
- **回归**：`test_death_event_passes_death_filter`（图内）+ test_events.py 的
  session 层版本 + `test_other_events_still_filtered_when_dead`

---

## P1 — 竞态 / 体验受损

### B7. 断连后 urgent 后台流水线继续跑，污染下一局（agent）

- **位置**：`agent/services/session.py:63-78`
- **根因**：`finally` 只清了 queue handler；`_spawn_coaching` 拉起的 urgent
  任务从不取消——入队到润色完成有 1-3s 窗口，期间断连，这些任务继续向已断
  会话 `send_text`、向 overlay 广播、往 Redis 写反馈上下文（污染下一局置信度）、
  虚增 `tips_published`。
- **修复**：`finally` 先 `cancel()` 全部后台任务再 `gather(return_exceptions=True)`，
  然后才清 handler（顺序保证取消前到达的批次不会回调死会话）；
  `handle_coaching` 开头对已 DISCONNECTED 的连接早退。
- **回归**：`test_background_tasks_cancelled_on_disconnect`

### B8. 广播串行 + 每客户端 5s 超时：慢 overlay 拖垮流式流水线（agent）

- **位置**：`agent/services/broadcast.py:21-38`
- **根因**：`for client in ...: await wait_for(send_text, 5)` 串行——流式路径
  每 0.1s 一次 emitter，一个卡死的客户端能让 LLM 迭代停摆 5s，一次润色被
  拉长到十几秒，且 `wait_for` 取消可能留下字节流失步。
- **修复**：`broadcast_tip_json` 改 `gather` 并发 + 单客户端 2s 超时，失败立即
  摘除；新增 `PolishStreamBroadcaster`（broadcast.py:40）：emit 只更新内存最新帧
  （O(1)，不碰 socket），单 worker 统一发送，**LLM 迭代与网络 IO 彻底解耦**；
  帧带 TTL（10s）防 worker 卡死时的过期文本上屏；`discard(tip_id)` 供流水线
  异常后清孤儿帧。lifespan 负责 worker 的 start/stop。
- **回归**：`test_slow_client_does_not_block_fast_client` /
  `test_stream_broadcaster_coalesces_and_relates_tip_id` /
  `test_stream_broadcaster_discard_prevents_orphan_send`

### B9. 流水线无端到端超时，流错误静默吞掉（agent）

- **位置**：`agent/services/session.py:67`、`agent/llm/openai_client.py:123-158`
- **根因**：`graph.ainvoke` 无 `wait_for`，而 achat 内部 3 次尝试 ×20s + 退避
  最坏 ~63s，×4 并发可占死 urgent 信号量；`achat_stream` 中途异常只 log 不
  关流、调用方拿到空 pieces 无法区分"API 故障"与"模型返回空"。
- **修复**：`handle_coaching` 加 `asyncio.wait_for(..., PIPELINE_TIMEOUT_S=30)`
  超时计 `graph_errors` 并丢弃；`achat_stream` 用 try/finally 显式关闭底层
  HTTP 响应，中断时**向上抛**（调用方据此保留部分文本而非静默回退草稿）；
  新增公开 `is_available()` / `aclose()`（替代节点里用私有 `_get_async_client`）。
- **回归**：`test_stream_mid_failure_keeps_partial_text`；
  `llm_polish` 无 LLM 时仍走草稿（既有用例）

### B10. 半开连接让 voice 客户端永久静默挂死（voice）

- **位置**：`voice/voice_broadcast.py:429`（原 304）
- **根因**：`ping_interval=None` 关掉库级 keepalive，改用应用层**单向** ping——
  agent 的 overlay 处理器收 ping 只 continue，不回 pong 也无超时判定。休眠/
  切网/Docker pause 后 `async for raw in ws` 在无数据、无 FIN 的半开 socket 上
  永久阻塞：不重连、不报错，必须手动重启。
- **修复**：`websockets.connect(url, ping_interval=20, ping_timeout=20)`——
  uvicorn 自动应答协议级 ping，死链 20s 内被发现并重连；删除无用的应用层
  心跳任务。
- **回归**：`test_connect_uses_library_keepalive`

### B11. 溢出丢弃按到达序杀掉队首紧急条（voice）

- **位置**：`voice/voice_broadcast.py:74-101`
- **根因**：满员丢弃取全局 seq 最小的条目——队列全是 urgent 时，被丢的正是
  突发中**最早到达**的紧急 tip（开团/斩杀线这类最先触发的信息）；而 agent 的
  25s skill 冷却让它 25s 内不会补推 = 永久丢失。
- **修复**：`_drop_victim_index()` 先丢最旧的普通条，全是紧急条才丢最旧紧急条；
  紧急丢失单独计数 `skipped_overflow_urgent` + warning 日志（丢普通条与丢紧急条
  不是一回事）。
- **回归**：`test_overflow_drops_oldest_normal_before_urgent` /
  `test_handle_tip_counts_urgent_overflow_separately`

### B12. publish() 中 SendState 失败即 return → 本 tick 事件全丢（collector）

- **位置**：`collector/cmd/main.go:188-229`
- **根因**：SendState 失败就 return，后面的 SendEvent 循环不执行——断线那一瞬
  检测到的 death/low_health 既没发出也没进 ring buffer，永久丢失。
- **修复**：记录首个 error 后继续遍历（每个 SendEvent 都会各自缓冲），最后统一
  返回第一个 error；**state 仍先于同 tick 事件**（agent 时效性判断依赖快照先到）；
  `lastStateSent` 仅成功后推进。
- **回归**：`TestPublish_SendStateFailureKeepsEvents`（离线 3 tick → 重连后回放
  帧序证明没丢）/ `TestPublish_StateArrivesBeforeEvents`

### B13. LCU 重连清 lastPhase → 伪 phase change 与重复 game_end（collector）

- **位置**：`collector/internal/lcu/poller.go:112,151`
- **根因**：重连时清空 `lastPhase/lastCSPhase/myPickDone`，注释说避免伪事件，
  实际保证了下一次 poll 必然发伪 `gameflow_phase_change`：InProgress 时重连会
  用选人阶段的旧 runes 覆写 agent 记忆；EndOfGame 时重连会重复 `game_end`
  （旁路 engine 冷却，review skill 可能跑两次复盘）。
- **修复**：重连不清空，改 `syncPhase()` 静默读取当前 phase 再进正常轮询；
  `game_end` 按 gameId 去重（`lastEndGameID`，同 gameId 只发一次；进入
  InProgress 重置让下一局能再发）。
- **回归**：`TestPoller_ReconnectKeepsPhaseTrackers` /
  `TestPoller_ReconnectAdoptsCurrentPhaseSilently` /
  `TestPoller_GameEndOncePerGame`

---

## P2 / P3 — 健壮性与性能

### B14. LCU 单次传输错误即断线 + 30s 重连节流（collector）

- **位置**：`collector/internal/lcu/client.go:30-90`、`poller.go:112-148`
- **修复**：连续 3 次 transport error 才标记断开（完成的请求含 404/599 清连击）；
  重连指数退避 2s→4s→8s→16s→30s，成功后归零。一次超时不再丢 30s LCU 数据。
- **回归**：`TestClient_TransportFailuresNeedThreeInARow` /
  `TestClient_SuccessClearsFailureStreak` / `TestPoller_ReconnectBackoffGrows` /
  `TestPoller_ReconnectResetsBackoff`

### B15. state 帧携带全量事件历史 → 帧体积线性膨胀（collector）

- **位置**：`collector/internal/lol/parser.go:13`、`cmd/main.go:197`
- **根因**：30 分钟局 ~1000+ 条事件，每帧全量序列化（100KB+），agent 每帧全量
  校验 + 全量写 Redis，带宽/CPU/Redis 三头放大。
- **修复**：`Events` 加 `omitempty`；publish 发**浅拷贝**且 `Events=nil`
  （不用原地置 nil——detector 通过 lastState 持同一指针）；`parseEvents` 增量
  解析（只取水位后新事件）。agent 侧 `events` 有默认空列表且无代码读取，缺键安全。
- **回归**：`TestPublish_StateFrameOmitsEventHistory` /
  `TestParseGameState_MissingSlicesSerializeAsEmptyArray`

### B16. LCU Get/GetArray 未排空 body → keep-alive 失效（collector）

- **位置**：`collector/internal/lcu/client.go:289,324,328`
- **根因**：json.Decoder 只读一部分，`defer Close` 时未到 EOF，连接无法复用——
  选人阶段每 2s 2-3 个请求 = 每分钟 ~100 次 TCP+TLS 握手。
- **修复**：Decode 后 `drainBody`（限幅 1MB）。
- **回归**：`TestClient_ReusesConnection` / `TestClientGetArray_ReusesConnection`
  （计数 listener 断言 5 次请求只建 1 条 TCP；回滚后实测变 4 条）

### B17. 等待开局时每秒全量枚举系统进程（collector）

- **位置**：`collector/internal/lol/client.go:72-142`
- **根因**：凭据不可用时每 tick `RefreshCredentials` → `process.Processes()`
  逐进程 `Name()`，macOS 数百 sysctl/秒。
- **修复**：lockfile 路径（1 stat + 1 read）不节流；进程枚举按 5s 基础节流、
  失败翻倍封顶 60s；401 失效凭据时清节流立即重扫。
- **回归**：`TestRefreshCredentials_ThrottlesProcessScan` /
  `TestRefreshCredentials_LockfileNeverThrottled` /
  `TestRefreshCredentials_ScanSuccessResetsBackoff`

### B18. 意外阻塞事件循环的同步调用（voice）

- **位置**：`voice/voice_broadcast.py:439`（原 312）
- **根因**：重连退避用 `threading.Event.wait(backoff)` 在跑事件循环的线程里
  同步阻塞 2→30s：心跳停摆、`asyncio.run` 清理被拖住、无法从协程侧取消。
- **修复**：`await asyncio.sleep(backoff)`；`run()` 整体 try/finally 保证
  任何退出路径都清理。
- **回归**：`test_connect_uses_library_keepalive`（覆盖 run 全路径）

### B19. 一条畸形帧打掉连接（voice / agent）

- **位置**：`voice/voice_broadcast.py:355-372`、`agent/services/session.py:54,156,174`
- **根因**：voice 只捕获 JSONDecodeError——合法 JSON 非对象、payload 非 dict、
  priority 非数字都会逃逸成异常 → 断线 + 重连风暴；agent 侧一条畸形帧直接
  杀死会话（等价于人为断连，触发假复盘）。
- **修复**：voice 对所有帧做 isinstance/类型降级并计 `malformed` 指标；
  agent 的 WSMessage/GameState/CoachEvent 校验失败只丢帧计 `frames_malformed`，
  会话存活。
- **回归**：`test_listen_ignores_tip_stream_and_malformed` /
  `test_malformed_frame_does_not_kill_session`

### B20. PowerShell 回退路径：抢焦点 / 不可打断 / 谎报指标（voice）

- **位置**：`voice/voice_broadcast.py:257-310`
- **修复**：`CREATE_NO_WINDOW`（后台常驻不再弹窗抢游戏焦点）；`Popen` + 轮询 +
  `kill()` 实现可打断（紧急 tip 可插队）；超时 120s→45s；启动失败/被打断
  返回 False（不再谎报"完整播出"）。
- **回归**：`test_powershell_returns_false_when_start_fails` /
  `test_powershell_killed_on_interrupt` / `test_powershell_hides_window_on_windows`

### B21. SAPI 路径：修补 Flag / 中途异常重播 / 初始化永久闭锁（voice）

- **位置**：`voice/voice_broadcast.py:185-256`
- **修复**：紧急条用 `SVSFlagsAsync|SVSFPurgeBeforeSpeak`（清掉 SAPI 请求队列
  残余，不会被前面未播完的流挡住）；区分"发起即失败"（可安全回退重播）与
  "播报中途异常"（放弃该条，避免半截重复朗读）；`_sapi_failed` 永久闭锁改
  30s 冷却重试（音频设备瞬时故障不该让会话永久降级）；线程退出
  `CoUninitialize`。
- **回归**：`test_sapi_interrupt_skips_and_returns_false` /
  `test_sapi_urgent_purges_queue` /
  `test_sapi_start_failure_falls_back_to_powershell` /
  `test_sapi_init_failure_is_not_permanent`

### B22. 打断置位被误清 / stop 拖死 / 重复调用（voice）

- **位置**：`voice/voice_broadcast.py:478、355-364、108`
- **修复**：`_interrupt.clear()` 移到 `get_item()` **之前**（取件与 clear 之间
  到达的紧急置位不再被擦掉——那是"紧急 tip 迟到 5-10s"的直接原因）；
  `stop()` 置 interrupt 让当前普通播报立即让路 + join 超时 5s；`close()` 幂等；
  `start()` 防重复；`--min-priority` 校验 1-3。
- **回归**：`test_min_priority_validated` / `test_run_stop_is_idempotent`
  （既有 `test_tts_urgent_interrupts_current_speech` 等在新顺序下仍通过）

### B23. 跨局状态不重置：新局开局建议被旧局静音（agent）

- **位置**：`agent/services/events.py:68-110`、`agent/memory/redis_store.py:97`
- **根因**：`conf:*` TTL 24h、tip 冷却按 skill cooldown——连打两局时第二局前
  几分钟的建议直接不发；`lcu_game_start`（天然的新局信号）到来时无任何清理。
- **修复**：`handle_lcu_event("lcu_game_start")` 调 `reset_session()`：SCAN
  删除该 session 的 `tip:*` / `conf:*` / `last_advice` / `state` 键（含进程内
  兜底模式的同步清理），并清空 `top_of_mind` 与对局期上下文，计
  `session_resets` 指标。`handle_lcu_event` 改 async。
- **回归**：`test_lcu_game_start_resets_session_state` /
  `test_lcu_other_events_do_not_reset`

### B24. Redis 无 socket 超时 + 每帧串行写入（agent）

- **位置**：`agent/memory/redis_store.py:45`、`agent/services/session.py:158`
- **根因**：`redis.asyncio` 默认无超时——Redis 卡住时 `_on_state` 的两个 await
  无限挂起，WS 循环停摆、队列不再消费，collector 毫无感知。
- **修复**：`from_url(socket_connect_timeout=2, socket_timeout=2)`
  （redis-py 的 TimeoutError 是 RedisError 子类，既有 except 自动降级）；
  `_on_state` 两个 Redis 操作 `gather` 并发。
- **回归**：既有反馈闭环/会话测试在假 redis 下通过；超时路径由异常降级保证

### B25. 队列 drain 竞态与静默吞批（agent）

- **位置**：`agent/memory/queue.py:117-160`
- **根因**：handler 检查在取批**之后**——断连竞态下批次被取出后直接 return，
  既不处理也不回填，静默消失且无计数；`max_per_window` 截断的丢弃也不可见。
- **修复**：handler 检查移到取批之前；`_flush_event.clear()` 改在取批前
  （取批后到达的 set() 保留到下一轮）；超额截断计 `metrics["tips_dropped"]`。
- **回归**：`test_handler_checked_before_batch_taken` /
  `test_max_per_window_excess_counted`

### B26. 未校验事件数据炸掉整条图（agent）

- **位置**：`agent/planner/planner.py:23,31-83`
- **根因**：`event.data` 是 Go `map[string]interface{}` 反序列化结果，Python 侧
  零校验——一个字符串/null 字段就能让 `f"{x:.0f}"` 抛 ValueError，整个 tip
  丢失而非降级。
- **修复**：`_num(data, key, default)` 安全取数，全部 6 个格式化点切换；
  不可转换时 warning + 默认值兜底。
- **回归**：`test_num_coerces_valid_values` / `test_num_falls_back_on_garbage` /
  `test_plan_survives_garbage_event_data`

### B27. 多 collector 并发无保护 / 生命周期资源泄漏 / 指标失真（agent）

- **位置**：`agent/routers/ws.py:50`、`agent/app.py:43-70`、`agent/context.py:30`、`routers/http.py:16`
- **修复**：
  - collector 连接单飞：已有活跃会话时拒绝新连接（close 1008）；
  - lifespan 关闭时 `gather` 等待被取消任务真正结束，并显式
    `aclose()` Redis / AsyncOpenAI / Embedder 连接池（`--reload` 每次
    重载漏一批的问题）；
  - metrics 补 `session_aborts` / `session_resets` / `tips_dropped` /
    `urgent_dropped` / `frames_malformed` 与 skip 原因分维度计数；
    `/health` 增加 `runtime`（overlay 客户端数 / 队列深度 / 断路器状态）。
- **回归**：`test_transient_disconnect_skips_review_and_record`（aborts 计数）/
  skip 分维度由 `test_reject_emits_tip_cancel` 等覆盖

### B28. Go 侧三处小正确性（collector）

- **位置**：`collector/internal/lcu/poller.go:236,446`（floatValOr）、
  `poller.go:344,188`（int64Val）、`collector/cmd/main.go:85,245`（POLL_INTERVAL）
- **修复**：`floatVal` 多键"回退"根本不生效（timer 缺失返回 0 而非备用键）→
  显式 `floatValOr`；毫秒时间戳/gameId 走 `float64→int` 32 位溢出 → `int64Val`；
  `POLL_INTERVAL=0` 能过校验 → `time.NewTicker(0)` panic → env 覆盖后 + runLoop
  入口双重兜底。
- **回归**：`TestFloatValOr_FallbackKeys` / `TestInt64Val_KeepsMillisecondPrecision` /
  `TestApplyEnvOverrides_PollInterval` / `TestRunLoop_ClampsInvalidInterval`

### B29. 未开游戏与抓取失败的日志/重置语义（collector）

- **位置**：`collector/internal/lol/client.go:352`、`collector/cmd/main.go:174`
- **修复**：`IsNotInGame` 增加 `errors.Is(err, syscall.ECONNREFUSED)` 判定
  （Windows 中文镜像/Linux 变体下字符串匹配会失效）；抓取失败日志限速
  （首次即打、之后每 10s 一行带连续计数），不再每 tick 刷屏 + 盲目 `Reset()`。
- **回归**：`TestIsNotInGame` / `TestLogFetchError_IsRateLimited`

---

## 测试与门禁变化

| 项目 | 变化 |
|------|------|
| agent 测试 | 55 → **80**（新增 test_events / test_planner / test_graph；扩充 session/queue/broadcast/nodes） |
| voice 测试 | 13 → **26**（假 COM/PowerShell 跨平台可测；畸形帧/keepalive/优先级丢弃覆盖） |
| collector 测试 | 新增 sender 写超时组、lcu 重连/streak 组、dragon spawn 身份组、main publish 组等 |
| CI | Go 测试开 `-race`（并发问题只有它能发现）；新增 **voice job**（此前 13 个测试既不在 CI 跑也无法在只装 voice 依赖的环境运行） |
| 依赖 | `voice/requirements.txt` 补 pytest / pytest-asyncio；新增 `voice/ruff.toml` |
| 仓库卫生 | `agent/knowledge/chroma_data/`（20MB HNSW 二进制，30 文件）`git rm --cached` 取消跟踪——`.gitignore` 早有规则但文件已被跟踪，规则对已跟踪文件无效；本地文件保留 |

顺带修复了 HEAD 上两个潜伏的 lint 失败（新版 ruff 才会报）：`lru_cache(maxsize=None)`
→ `functools.cache`（UP033）、多余引号注解（UP037）。

## 未做项（建议，未动手）

- **CORS `allow_origins=["*"]` + WS 端点无鉴权**：本机服务风险有限，建议收紧到
  overlay 源并评估 token。属安全加固而非 bug，待产品定位明确后再做。
- **多用户/session_id 体系**：当前全程硬编码 `"default"`，单飞保护是权宜之计；
  真正的多采集器支持需要 session 上下文改造。
- **overlay 前端**（浏览器）不在本仓库，`tip_cancel`/`tip_id` 协议已就绪，
  前端接入时需要实现卡片归属与撤回渲染。
