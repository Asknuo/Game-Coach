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

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if !client.IsAvailable() {
				if err := client.RefreshCredentials(); err != nil {
					log.Printf("waiting for lol client: %v", err)
					continue
				}
				log.Println("lol live client connected")
			}

			state, err := client.FetchGameState(ctx)
			if err != nil {
				log.Printf("fetch state: %v", err)
				// Reset detector on fetch failure to avoid stale-state
				// jump on next success (periodic checks rely on lastState).
				engine.Reset()
				continue
			}

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
