package lol

import (
	"encoding/json"
	"fmt"
	"time"
)

type GameState struct {
	GameTime       float64        `json:"game_time"`
	ActivePlayer   ActivePlayer   `json:"active_player"`
	AllPlayers     []Player       `json:"all_players"`
	Events         []GameEvent    `json:"events"`
	DragonTimer    *DragonInfo    `json:"dragon_timer,omitempty"`
	BaronTimer     *BaronInfo     `json:"baron_timer,omitempty"`
	CollectedAt    time.Time      `json:"collected_at"`
	RawEventData   json.RawMessage `json:"-"`
}

type ActivePlayer struct {
	SummonerName string  `json:"summoner_name"`
	Level        int     `json:"level"`
	CurrentGold  float64 `json:"current_gold"`
	Health       float64 `json:"health"`
	MaxHealth    float64 `json:"max_health"`
	Position     Vec2    `json:"position"`
	Items        []Item  `json:"items"`
}

type Player struct {
	SummonerName string  `json:"summoner_name"`
	Team         string  `json:"team"`
	ChampionName string  `json:"champion_name"`
	Level        int     `json:"level"`
	Kills        int     `json:"kills"`
	Deaths       int     `json:"deaths"`
	Assists      int     `json:"assists"`
	CurrentGold  float64 `json:"current_gold"`
	CreepScore   int     `json:"creep_score"`
	Health       float64 `json:"health"`
	MaxHealth    float64 `json:"max_health"`
	Position     Vec2    `json:"position"`
	Items        []Item  `json:"items"`
}

type Item struct {
	ItemID int `json:"item_id"`
	Slot   int `json:"slot"`
}

type Vec2 struct {
	X float64 `json:"x"`
	Y float64 `json:"y"`
}

type GameEvent struct {
	EventID    int     `json:"event_id"`
	EventName  string  `json:"event_name"`
	EventTime  float64 `json:"event_time"`
	DragonType string  `json:"dragon_type,omitempty"`
}

type DragonInfo struct {
	Type       string  `json:"type"`
	SpawnTime  float64 `json:"spawn_time"`
	SecondsLeft float64 `json:"seconds_left"`
}

type BaronInfo struct {
	SpawnTime   float64 `json:"spawn_time"`
	SecondsLeft float64 `json:"seconds_left"`
}

func ParseGameState(raw []byte) (*GameState, error) {
	var root map[string]json.RawMessage
	if err := json.Unmarshal(raw, &root); err != nil {
		return nil, fmt.Errorf("parse root: %w", err)
	}

	state := &GameState{
		CollectedAt: time.Now().UTC(),
	}

	if data, ok := root["gameData"]; ok {
		var gd struct {
			GameTime float64 `json:"gameTime"`
		}
		if err := json.Unmarshal(data, &gd); err == nil {
			state.GameTime = gd.GameTime
		}
	}

	if data, ok := root["activePlayer"]; ok {
		var ap struct {
			SummonerName string `json:"summonerName"`
			Level        int    `json:"level"`
			CurrentGold  float64 `json:"currentGold"`
			ChampionStats struct {
				CurrentHealth float64 `json:"currentHealth"`
				MaxHealth     float64 `json:"maxHealth"`
			} `json:"championStats"`
			Items []struct {
				ItemID int `json:"itemID"`
				Slot   int `json:"slot"`
			} `json:"items"`
		}
		if err := json.Unmarshal(data, &ap); err == nil {
			items := make([]Item, 0, len(ap.Items))
			for _, it := range ap.Items {
				items = append(items, Item{ItemID: it.ItemID, Slot: it.Slot})
			}
			state.ActivePlayer = ActivePlayer{
				SummonerName: ap.SummonerName,
				Level:        ap.Level,
				CurrentGold:  ap.CurrentGold,
				Health:       ap.ChampionStats.CurrentHealth,
				MaxHealth:    ap.ChampionStats.MaxHealth,
				Items:        items,
			}
		}
	}

	if data, ok := root["allPlayers"]; ok {
		var players []struct {
			SummonerName string  `json:"summonerName"`
			Team         string  `json:"team"`
			ChampionName string  `json:"championName"`
			Level        int     `json:"level"`
			CurrentGold  float64 `json:"currentGold"`
			Scores       struct {
				Health     float64 `json:"health"`
				MaxHealth  float64 `json:"maxHealth"`
				Kills      int     `json:"kills"`
				Deaths     int     `json:"deaths"`
				Assists    int     `json:"assists"`
				CreepScore int     `json:"creepScore"`
			} `json:"scores"`
			Position struct {
				X float64 `json:"x"`
				Y float64 `json:"y"`
			} `json:"position"`
			Items []struct {
				ItemID int `json:"itemID"`
				Slot   int `json:"slot"`
			} `json:"items"`
		}
		if err := json.Unmarshal(data, &players); err == nil {
			for _, p := range players {
				items := make([]Item, len(p.Items))
				for i, it := range p.Items {
					items[i] = Item{ItemID: it.ItemID, Slot: it.Slot}
				}
				state.AllPlayers = append(state.AllPlayers, Player{
					SummonerName: p.SummonerName,
					Team:         p.Team,
					ChampionName: p.ChampionName,
					Level:        p.Level,
					Kills:        p.Scores.Kills,
					Deaths:       p.Scores.Deaths,
					Assists:      p.Scores.Assists,
					CurrentGold:  p.CurrentGold,
					CreepScore:   p.Scores.CreepScore,
					Health:       p.Scores.Health,
					MaxHealth:    p.Scores.MaxHealth,
					Position:     Vec2{X: p.Position.X, Y: p.Position.Y},
					Items:        items,
				})
			}
		}
	}

	if data, ok := root["events"]; ok {
		state.RawEventData = data
		var ev struct {
			Events []apiGameEvent `json:"Events"`
		}
		if err := json.Unmarshal(data, &ev); err == nil {
			state.Events = make([]GameEvent, len(ev.Events))
			for i, e := range ev.Events {
				state.Events[i] = e.toGameEvent()
			}
		}
	}

	return state, nil
}

type apiGameEvent struct {
	EventID    int     `json:"EventID"`
	EventName  string  `json:"EventName"`
	EventTime  float64 `json:"EventTime"`
	DragonType string  `json:"DragonType"`
}

func (e apiGameEvent) toGameEvent() GameEvent {
	return GameEvent{
		EventID:    e.EventID,
		EventName:  e.EventName,
		EventTime:  e.EventTime,
		DragonType: e.DragonType,
	}
}

func (s *GameState) ActivePlayerHealthPct() float64 {
	if s.ActivePlayer.MaxHealth <= 0 {
		return 100
	}
	return s.ActivePlayer.Health / s.ActivePlayer.MaxHealth * 100
}
