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
			cfg.PollInterval = d
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
	// 零值 → 游戏开始后首个 tick 必发一帧 state，让 Agent 立即拿到快照
	lastStateSent time.Time
}

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
				log.Println("game data: waiting for game to start...")
			}
			return nil, false
		}
		log.Printf("fetch state error: %v", err)
		l.engine.Reset()
		return nil, false
	}

	if !l.gameNotified {
		l.gameNotified = true
		log.Println("game data: game started, streaming state + events")
	}
	l.notInGameLogged = false
	return state, true
}

// publish 处理事件检测并按事件驱动 + 心跳策略发送 state / events。
func (l *collectLoop) publish(state *lol.GameState) error {
	state.MergeActivePlayer()
	events := l.engine.Process(state)

	// 事件驱动发送：有事件时 state 随事件一起发（state 是事件的上下文）；
	// 无事件时仅靠心跳刷新，避免每秒全量推送（一局 ~1800 条冗余消息）
	if len(events) > 0 || time.Since(l.lastStateSent) >= stateHeartbeat {
		if err := l.ws.SendState(state); err != nil {
			return err
		}
		l.lastStateSent = time.Now()
	}

	for _, ev := range events {
		if err := l.ws.SendEvent(ev); err != nil {
			return err
		}
		log.Printf("event: %s", ev.Name)
	}
	return nil
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
