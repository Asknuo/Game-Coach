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

func runLoop(ctx context.Context, client *lol.Client, engine *event.Engine, ws *sender.WebSocket, interval time.Duration) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	credsNotified := false
	gameNotified := false
	notInGameLogged := false

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if !client.HasCredentials() {
				if err := client.RefreshCredentials(); err != nil {
					if !credsNotified {
						credsNotified = true
						log.Printf("game data: %v", err)
					}
					continue
				}
				credsNotified = false
				gameNotified = false
				notInGameLogged = false
				log.Println("game data: live client credentials ready (port 2999)")
			}

			state, err := client.FetchGameState(ctx)
			if err != nil {
				if err == lol.ErrNotInGame || lol.IsNotInGame(err) {
					if !notInGameLogged {
						notInGameLogged = true
						log.Println("game data: waiting for game to start...")
					}
					continue
				}
				log.Printf("fetch state error: %v", err)
				engine.Reset()
				continue
			}

			if !gameNotified {
				gameNotified = true
				log.Println("game data: game started, streaming state + events")
			}
			notInGameLogged = false

			state.MergeActivePlayer()

			if err := ws.SendState(state); err != nil {
				return err
			}

			for _, ev := range engine.Process(state) {
				if err := ws.SendEvent(ev); err != nil {
					return err
				}
				log.Printf("event: %s", ev.Name)
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
