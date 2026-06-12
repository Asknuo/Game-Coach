package main

import (
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

type Config struct {
	AgentWSURL      string        `yaml:"agent_ws_url"`
	PollInterval    time.Duration `yaml:"-"`
	PollIntervalSec float64       `yaml:"poll_interval_sec"`
	LockfilePath    string        `yaml:"lockfile_path"`
}

func loadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return defaultConfig(), nil
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}

	cfg.PollInterval = time.Duration(cfg.PollIntervalSec * float64(time.Second))
	if cfg.PollInterval <= 0 {
		cfg.PollInterval = time.Second
	}
	if cfg.AgentWSURL == "" {
		cfg.AgentWSURL = "ws://localhost:8000/ws/collector"
	}

	return &cfg, nil
}

func defaultConfig() *Config {
	return &Config{
		AgentWSURL:   "ws://localhost:8000/ws/collector",
		PollInterval: time.Second,
	}
}
