package main

import (
	"context"
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/game-coach/collector/internal/event"
	"github.com/game-coach/collector/internal/lcu"
	"github.com/game-coach/collector/internal/lol"
	"github.com/game-coach/collector/internal/sender"
)

func main() {
	configPath := flag.String("config", "config/config.yaml", "path to config file")
	flag.Parse()

	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}

	applyEnvOverrides(cfg)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("shutting down...")
		cancel()
	}()

	client := lol.NewClient(cfg.LockfilePath)
	engine := event.NewEngine(event.NewDetector())
	ws := sender.NewWebSocket(cfg.AgentWSURL)

	// LCU poller — lobby data (summoner, runes, masteries, champion select).
	lcuClient := lcu.NewClient()
	lcuPoller := lcu.NewPoller(lcuClient, func(name string, data map[string]interface{}) {
		if err := ws.SendEvent(event.Event{Name: name, Data: data}); err != nil {
			log.Printf("[LCU] send event failed: %v", err)
		} else {
			log.Printf("[LCU] event: %s", name)
		}
	})
	go lcuPoller.Run(ctx)

	log.Printf("collector starting, agent=%s poll=%s", cfg.AgentWSURL, cfg.PollInterval)

	for {
		if ctx.Err() != nil {
			return
		}

		if err := ws.Connect(ctx); err != nil {
			log.Printf("websocket connect failed: %v, retry in 3s", err)
			sleep(ctx, 3*time.Second)
			continue
		}

		if err := runLoop(ctx, client, engine, ws, cfg.PollInterval); err != nil {
			log.Printf("loop error: %v", err)
		}

		ws.Close()
		sleep(ctx, 2*time.Second)
	}
}

// applyEnvOverrides 用环境变量覆盖配置文件中的对应项。
func applyEnvOverrides(cfg *Config) {
	if v := os.Getenv("AGENT_WS_URL"); v != "" {
		cfg.AgentWSURL = v
	}
	if v := os.Getenv("POLL_INTERVAL"); v != "" {
		if d, err := time.ParseDuration(v + "s"); err == nil {
			// 0/负数能通过 ParseDuration，但会让 time.NewTicker panic
			if d <= 0 {
				log.Printf("WARNING: POLL_INTERVAL=%q must be > 0, using default %v", v, cfg.PollInterval)
			} else {
				cfg.PollInterval = d
			}
		} else {
			log.Printf("WARNING: invalid POLL_INTERVAL=%q, using default %v", v, cfg.PollInterval)
		}
	}
}

// stateHeartbeat: 无事件时定期刷新 Agent 快照的间隔。
// Agent 的建议反馈闭环（ADVICE_FEEDBACK_WINDOW=25s）依赖 state 帧驱动，
// 心跳间隔必须小于该窗口。
const stateHeartbeat = 20 * time.Second

// collectLoop 持有采集循环跨 tick 的可变状态与依赖，
// 把原本挤在一个大函数里的逻辑拆成多个低复杂度的方法。
type collectLoop struct {
	client *lol.Client
	engine *event.Engine
	ws     *sender.WebSocket

	credsNotified   bool
	gameNotified    bool
	notInGameLogged bool
	// 抓取失败降噪：连续失败次数与上次打日志的时间
	fetchErrors      int
	fetchErrorLogged time.Time
	// 零值 → 游戏开始后首个 tick 必发一帧 state，让 Agent 立即拿到快照
	lastStateSent time.Time
}

// fetchErrorLogInterval 控制 fetch 失败日志的降噪节奏。
const fetchErrorLogInterval = 10 * time.Second

// ensureCredentials 确保 live client 凭据可用，返回 false 表示本 tick 应跳过。
func (l *collectLoop) ensureCredentials() bool {
	if l.client.HasCredentials() {
		return true
	}
	if err := l.client.RefreshCredentials(); err != nil {
		if !l.credsNotified {
			l.credsNotified = true
			log.Printf("game data: %v", err)
		}
		return false
	}
	l.credsNotified = false
	l.gameNotified = false
	l.notInGameLogged = false
	log.Println("game data: live client credentials ready (port 2999)")
	return true
}

// fetchState 拉取游戏状态，第二个返回值为 false 表示本 tick 无有效 state。
func (l *collectLoop) fetchState(ctx context.Context) (*lol.GameState, bool) {
	state, err := l.client.FetchGameState(ctx)
	if err != nil {
		if err == lol.ErrNotInGame || lol.IsNotInGame(err) {
			if !l.notInGameLogged {
				l.notInGameLogged = true
				// 局间空窗必须清 detector 状态：否则上局 dragonWarned/baronWarned
				// 锁存与敌方基线残留到下一局（此分支不会进 Detect，无自愈机会）
				l.engine.Reset()
				log.Println("game data: waiting for game to start...")
			}
			return nil, false
		}
		// 抓取失败（401/超时）与"未开游戏"分开：前者需要降噪，
		// 否则一次持续故障每秒一行错误会淹没日志
		l.engine.Reset()
		l.logFetchError(err)
		return nil, false
	}

	l.fetchErrors = 0
	l.fetchErrorLogged = time.Time{}

	if !l.gameNotified {
		l.gameNotified = true
		log.Println("game data: game started, streaming state + events")
	}
	l.notInGameLogged = false
	return state, true
}

// logFetchError 报告连续抓取失败：第一条立即打，之后按 fetchErrorLogInterval
// 降噪（401 刷屏/网络抖动期间最多每 10s 一行）。
func (l *collectLoop) logFetchError(err error) {
	l.fetchErrors++
	switch {
	case l.fetchErrors == 1:
		l.fetchErrorLogged = time.Now()
		log.Printf("fetch state error: %v (retrying; repeated failures are logged every %s)",
			err, fetchErrorLogInterval)
	case time.Since(l.fetchErrorLogged) >= fetchErrorLogInterval:
		l.fetchErrorLogged = time.Now()
		log.Printf("fetch state error: %v (%d consecutive failures)", err, l.fetchErrors)
	}
}

// publish 处理事件检测并按事件驱动 + 心跳策略发送 state / events。
func (l *collectLoop) publish(state *lol.GameState) error {
	state.MergeActivePlayer()
	events := l.engine.Process(state)

	// Events 携带整局事件历史（30 分钟局单帧 100KB+），而 Agent 每帧都全量校验
	// 并写 Redis。发出去的帧不含它：omitempty + 显式置 nil。
	// 这里用浅拷贝而不是原地置 nil —— detector 通过 lastState 持有同一个
	// *GameState（虽然当前只读 GameTime），清空原值会让那条隐式契约失效。
	out := *state
	out.Events = nil

	var firstErr error
	fail := func(err error) {
		// 记录首个 error，剩余的发送继续执行
		if err != nil && firstErr == nil {
			firstErr = err
		}
	}

	// 事件驱动发送：有事件时 state 随事件一起发（state 是事件的上下文）；
	// 无事件时仅靠心跳刷新，避免每秒全量推送（一局 ~1800 条冗余消息）
	if len(events) > 0 || time.Since(l.lastStateSent) >= stateHeartbeat {
		// state 必须先于同 tick 的事件：Agent 侧的 dragon_timer 时效性判断
		// 依赖快照先于引用它的事件到达。
		// SendState 失败不能 early-return，否则本 tick 检出的事件既没发出
		// 也没进 ring buffer（send 失败即入 buffer 的路径被跳过），永久丢失。
		if err := l.ws.SendState(&out); err != nil {
			fail(err)
		} else {
			l.lastStateSent = time.Now()
		}
	}

	for _, ev := range events {
		if err := l.ws.SendEvent(ev); err != nil {
			fail(err)
			continue // 断线时后续事件也必须继续入 ring buffer
		}
		log.Printf("event: %s", ev.Name)
	}
	return firstErr
}

// tick 执行单次采集：凭据检查 → 拉取状态 → 发送。
func (l *collectLoop) tick(ctx context.Context) error {
	if !l.ensureCredentials() {
		return nil
	}
	state, ok := l.fetchState(ctx)
	if !ok {
		return nil
	}
	return l.publish(state)
}

func runLoop(ctx context.Context, client *lol.Client, engine *event.Engine, ws *sender.WebSocket, interval time.Duration) error {
	// 兜底：POLL_INTERVAL 即使绕过校验也不能让 NewTicker panic
	if interval <= 0 {
		interval = time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	loop := &collectLoop{client: client, engine: engine, ws: ws}

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := loop.tick(ctx); err != nil {
				return err
			}
		}
	}
}

func sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}
