package lcu

import (
	"context"
	"log"
	"time"
)

const (
	pollInterval = 2 * time.Second

	// Reconnect backoff doubles per failed attempt (2s → 4s → … → 30s). The old
	// flat 30s throttle meant one blip silenced LCU data for half a minute.
	reconnectBackoffBase = 2 * time.Second
	reconnectBackoffMax  = 30 * time.Second

	// noGameID marks "game_end was never sent" — gameIds are > 0 in practice.
	noGameID = int64(-1)
)

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
	lastEndGameID   int64
	reconnectFails  int
	lastReconnect   time.Time
	latestSummoner  *SummonerInfo
	latestRunes     *RunePage
	latestMasteries []ChampionMastery
}

func NewPoller(client *Client, cb Callback) *Poller {
	return &Poller{
		client:        client,
		callback:      cb,
		lastEndGameID: noGameID,
	}
}

// Run starts the LCU polling loop. Blocks until ctx is done.
// If the initial connect fails, keeps retrying (throttled by reconnectPeriod).
func (p *Poller) Run(ctx context.Context) {
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
		log.Println("[LCU] unavailable — League Client not detected, retrying every 30s")
		p.lastReconnect = time.Now()
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
	if !p.client.Connected() {
		p.tryReconnect()
		return
	}

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

// tryReconnect retries the LCU connection with exponential backoff
// (2s → 4s → … → 30s). The client itself only gives up after
// maxTransportFailures consecutive transport errors, so most blips never even
// get here; the ones that do retry quickly instead of after a flat 30s.
func (p *Poller) tryReconnect() {
	if time.Since(p.lastReconnect) < p.reconnectBackoff() {
		return
	}
	p.lastReconnect = time.Now()
	if !p.client.TryConnect() {
		p.reconnectFails++
		return
	}
	p.reconnectFails = 0
	log.Println("[LCU] reconnected")
	// Deliberately keep the phase trackers: clearing lastPhase made the next
	// `phase != p.lastPhase` check trivially true, replaying lcu_game_start
	// (stale runes overwrote the agent's memory) or game_end (the whole review
	// ran a second time). Instead, silently adopt the current phase.
	p.syncPhase()
	p.fetchSummoner()
	p.fetchRunes()
	p.fetchMasteries()
}

// reconnectBackoff is the current reconnect wait: base doubled once per failed
// attempt, capped at reconnectBackoffMax.
func (p *Poller) reconnectBackoff() time.Duration {
	d := reconnectBackoffBase
	for i := 0; i < p.reconnectFails; i++ {
		if d >= reconnectBackoffMax {
			return reconnectBackoffMax
		}
		d *= 2
	}
	if d > reconnectBackoffMax {
		return reconnectBackoffMax
	}
	return d
}

// syncPhase reads the current gameflow phase into lastPhase WITHOUT emitting an
// event, so a reconnect looks like business as usual on the next poll.
func (p *Poller) syncPhase() {
	resp, err := p.client.Get("/lol-gameflow/v1/session")
	if err != nil || resp == nil {
		return
	}
	p.lastPhase = strVal(resp, "phase")
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

	// gameId is a ms-precision epoch value — float64 would lose precision on
	// 32-bit builds, so read it as int64.
	gameID := int64Val(gamedata, "gameId")

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
			// A new game started: allow game_end to fire for it again.
			p.lastEndGameID = noGameID
			p.onGameStart()
		}

		if phase == "EndOfGame" || phase == "WaitingForStats" {
			// game_end is deduplicated by gameId: the LCU walks through both
			// phases (and a reconnect used to replay them), which re-ran the
			// whole review twice.
			if gameID != p.lastEndGameID {
				p.lastEndGameID = gameID
				p.callback("game_end", map[string]interface{}{
					"phase":   phase,
					"game_id": gameID,
				})
			}
		}
	}
}

func (p *Poller) pollChampSelect() {
	resp, err := p.client.Get("/lol-champ-select/v1/session")
	if err != nil || resp == nil {
		return
	}

	// The champ-select session exposes the phase timer as a plain number under
	// "timer" or, when that key is absent, under "adjustedPositionInPhase".
	// Read the first key that exists — the old vararg helper treated the keys
	// as a nested path, so the fallback never applied.
	timer := floatValOr(resp, "timer", "adjustedPositionInPhase")
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
			LastPlayTime:   int64Val(m, "lastPlayTime"),
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

// int64Val reads a numeric field as int64. Used for millisecond epoch
// timestamps and gameIds: float64 → int loses precision on 32-bit builds
// (a 2026 timestamp does not fit in 32 bits).
func int64Val(m map[string]interface{}, key string) int64 {
	if v, ok := m[key]; ok {
		switch vv := v.(type) {
		case float64:
			return int64(vv)
		case int:
			return int64(vv)
		case int64:
			return vv
		}
	}
	return 0
}

// floatValOr returns the first float64 found among keys — all keys are read at
// the SAME level of m (a fallback list, not a nested path).
func floatValOr(m map[string]interface{}, keys ...string) float64 {
	for _, key := range keys {
		if f, ok := m[key].(float64); ok {
			return f
		}
	}
	return 0
}
