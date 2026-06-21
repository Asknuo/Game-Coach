package lcu

import (
	"context"
	"log"
	"time"
)

const pollInterval = 2 * time.Second

// Callback is invoked with LCU events (name + data).
type Callback func(name string, data map[string]interface{})

// Poller periodically queries LCU API endpoints and emits events.
type Poller struct {
	client   *Client
	callback Callback

	// State tracking.
	lastPhase       string
	lastCSPhase     string
	myPickDone      bool
	latestSummoner  *SummonerInfo
	latestRunes     *RunePage
	latestMasteries []ChampionMastery
}

func NewPoller(client *Client, cb Callback) *Poller {
	return &Poller{
		client:   client,
		callback: cb,
	}
}

// Run starts the LCU polling loop. Blocks until ctx is done.
// Tries LCU connect once; if it fails, gives up (no retry loop).
func (p *Poller) Run(ctx context.Context) {
	// Try once.
	if p.client.TryConnect() {
		summoner, err := p.client.Get("/lol-summoner/v1/current-summoner")
		if err == nil && summoner != nil {
			name := strVal(summoner, "displayName")
			if name == "" {
				name = strVal(summoner, "gameName")
			}
			log.Printf("[LCU] connected! summoner=%s, level=%.0f", name, summoner["summonerLevel"])
			p.callback("lcu_connected", map[string]interface{}{
				"summoner": summoner,
			})
			p.fetchSummoner()
		}
	} else {
		log.Println("[LCU] unavailable — League Client not detected, LCU will be skipped")
		return
	}

	for {
		select {
		case <-ctx.Done():
			log.Println("[LCU] poller stopped")
			return
		default:
		}

		p.poll(ctx)

		select {
		case <-ctx.Done():
			return
		case <-time.After(pollInterval):
		}
	}
}

func (p *Poller) poll(ctx context.Context) {
	// 1. Summoner info.
	p.fetchSummoner()

	// 2. GameFlow — most important.
	p.pollGameFlow()

	// 3. Champion Select.
	if p.lastPhase == "ChampSelect" {
		p.pollChampSelect()
	} else {
		p.myPickDone = false
	}
}

func (p *Poller) fetchSummoner() {
	resp, err := p.client.Get("/lol-summoner/v1/current-summoner")
	if err != nil || resp == nil {
		return
	}
	p.latestSummoner = &SummonerInfo{
		SummonerID:    intVal(resp, "summonerId"),
		AccountID:     intVal(resp, "accountId"),
		DisplayName:   strValOr(resp, "displayName", strVal(resp, "gameName")),
		SummonerLevel: intVal(resp, "summonerLevel"),
		ProfileIconID: intVal(resp, "profileIconId"),
		Puuid:         strVal(resp, "puuid"),
	}
}

func (p *Poller) pollGameFlow() {
	resp, err := p.client.Get("/lol-gameflow/v1/session")
	if err != nil || resp == nil {
		return
	}

	phase := strVal(resp, "phase")
	gamedata, _ := resp["gameData"].(map[string]interface{})
	if gamedata == nil {
		gamedata = map[string]interface{}{}
	}

	gameID := intVal(gamedata, "gameId")

	// Phase change detection.
	if phase != p.lastPhase {
		p.callback("gameflow_phase_change", map[string]interface{}{
			"old_phase": p.lastPhase,
			"new_phase": phase,
			"game_id":   gameID,
		})
		p.lastPhase = phase

		if phase == "ChampSelect" {
			p.myPickDone = false
			p.fetchRunes()
			p.fetchMasteries()
		}

		if phase == "InProgress" {
			p.onGameStart()
		}

		if phase == "EndOfGame" || phase == "WaitingForStats" {
			p.callback("game_end", map[string]interface{}{
				"phase":   phase,
				"game_id": gameID,
			})
		}
	}
}

func (p *Poller) pollChampSelect() {
	resp, err := p.client.Get("/lol-champ-select/v1/session")
	if err != nil || resp == nil {
		return
	}

	timer := floatVal(resp, "timer", "adjustedPositionInPhase")
	localID := intVal(resp, "localPlayerCellId")

	phase := ""
	actions, _ := resp["actions"].([]interface{})
	for _, actionList := range actions {
		list, ok := actionList.([]interface{})
		if !ok {
			continue
		}
		for _, act := range list {
			a, ok := act.(map[string]interface{})
			if !ok {
				continue
			}
			if intVal(a, "actorCellId") == localID {
				actionType := strVal(a, "type")
				inProgress, _ := a["isInProgress"].(bool)
				if actionType == "pick" && inProgress {
					phase = "picking"
				} else if actionType == "ban" && inProgress {
					phase = "banning"
				}
			}
		}
	}

	if phase != p.lastCSPhase {
		p.lastCSPhase = phase
		if phase == "picking" {
			p.callback("lcu_pick_phase", map[string]interface{}{
				"phase": phase,
				"timer": timer,
			})
		}
	}

	// Detect champion picked.
	myTeam, _ := resp["myTeam"].([]interface{})
	if myTeam != nil && !p.myPickDone {
		for _, m := range myTeam {
			member, ok := m.(map[string]interface{})
			if !ok {
				continue
			}
			if intVal(member, "cellId") == localID && intVal(member, "championId") > 0 {
				p.myPickDone = true
				p.callback("lcu_champion_picked", map[string]interface{}{
					"champion_id":       intVal(member, "championId"),
					"assigned_position": strVal(member, "assignedPosition"),
					"spell1_id":         intVal(member, "spell1Id"),
					"spell2_id":         intVal(member, "spell2Id"),
				})
				break
			}
		}
	}
}

func (p *Poller) fetchRunes() {
	resp, err := p.client.Get("/lol-perks/v1/currentpage")
	if err != nil || resp == nil {
		return
	}

	perkIDs := []int{}
	if rawIDs, ok := resp["selectedPerkIds"].([]interface{}); ok {
		for _, id := range rawIDs {
			if i, ok := id.(float64); ok {
				perkIDs = append(perkIDs, int(i))
			}
		}
	}

	p.latestRunes = &RunePage{
		ID:              intVal(resp, "id"),
		Name:            strVal(resp, "name"),
		PrimaryStyleID:  intVal(resp, "primaryStyleId"),
		SubStyleID:      intVal(resp, "subStyleId"),
		SelectedPerkIDs: perkIDs,
		IsActive:        boolVal(resp, "isActive"),
	}

	p.callback("lcu_runes_updated", map[string]interface{}{
		"primary_style_id": p.latestRunes.PrimaryStyleID,
		"sub_style_id":     p.latestRunes.SubStyleID,
		"perk_ids":         p.latestRunes.SelectedPerkIDs,
	})
}

func (p *Poller) fetchMasteries() {
	resp, err := p.client.GetArray("/lol-champion-mastery/v1/local-player/champion-mastery")
	if err != nil || resp == nil {
		return
	}

	limit := 20
	if len(resp) < limit {
		limit = len(resp)
	}

	masteries := make([]ChampionMastery, 0, limit)
	for i := 0; i < limit; i++ {
		m := resp[i]
		masteries = append(masteries, ChampionMastery{
			ChampionID:     intVal(m, "championId"),
			ChampionLevel:  intVal(m, "championLevel"),
			ChampionPoints: intVal(m, "championPoints"),
			LastPlayTime:   int64(intVal(m, "lastPlayTime")),
			ChestGranted:   boolVal(m, "chestGranted"),
		})
	}
	p.latestMasteries = masteries

	p.callback("lcu_mastery_loaded", map[string]interface{}{
		"count": len(masteries),
	})
}

func (p *Poller) onGameStart() {
	topMasteries := make([]map[string]interface{}, 0, len(p.latestMasteries))
	for _, m := range p.latestMasteries {
		topMasteries = append(topMasteries, map[string]interface{}{
			"champion_id": m.ChampionID,
			"level":       m.ChampionLevel,
			"points":      m.ChampionPoints,
		})
	}

	data := map[string]interface{}{
		"summoner_name":  "",
		"summoner_level": 0,
		"runes":          map[string]interface{}{},
		"top_masteries":  topMasteries,
	}
	if p.latestSummoner != nil {
		data["summoner_name"] = p.latestSummoner.DisplayName
		data["summoner_level"] = p.latestSummoner.SummonerLevel
	}
	if p.latestRunes != nil {
		data["runes"] = map[string]interface{}{
			"primary_style_id": p.latestRunes.PrimaryStyleID,
			"sub_style_id":     p.latestRunes.SubStyleID,
			"perk_ids":         p.latestRunes.SelectedPerkIDs,
		}
	}

	p.callback("lcu_game_start", data)
	log.Println("[LCU] game start context sent")
}

// ── JSON helpers ──

func intVal(m map[string]interface{}, key string) int {
	if v, ok := m[key]; ok {
		switch vv := v.(type) {
		case float64:
			return int(vv)
		case int:
			return vv
		}
	}
	return 0
}

func strVal(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return ""
}

func strValOr(m map[string]interface{}, key string, fallback string) string {
	v := strVal(m, key)
	if v == "" {
		return fallback
	}
	return v
}

func boolVal(m map[string]interface{}, key string) bool {
	if v, ok := m[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return false
}

func floatVal(m map[string]interface{}, keys ...string) float64 {
	var cur interface{} = m
	for i, key := range keys {
		if mm, ok := cur.(map[string]interface{}); ok {
			cur = mm[key]
		} else {
			if i == len(keys)-1 {
				if f, ok := cur.(float64); ok {
					return f
				}
			}
			return 0
		}
	}
	if f, ok := cur.(float64); ok {
		return f
	}
	return 0
}
