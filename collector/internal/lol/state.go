package lol

// GameState snapshot helpers live in parser.go alongside ParseGameState.
// This file holds derived-state utilities used by the event detector.

func (s *GameState) IsInGame() bool {
	return s.GameTime > 0 && s.ActivePlayer.SummonerName != ""
}

func (s *GameState) ActivePlayerTeam() string {
	for _, p := range s.AllPlayers {
		if p.SummonerName == s.ActivePlayer.SummonerName {
			return p.Team
		}
	}
	return ""
}

func (s *GameState) EnemyPlayers() []Player {
	team := s.ActivePlayerTeam()
	if team == "" {
		return nil
	}
	var enemies []Player
	for _, p := range s.AllPlayers {
		if p.Team != team {
			enemies = append(enemies, p)
		}
	}
	return enemies
}

func (s *GameState) ItemCount(player Player) int {
	count := 0
	for _, it := range player.Items {
		if it.ItemID > 0 {
			count++
		}
	}
	return count
}

// ActivePlayerFromAll returns the active player's entry from AllPlayers.
// Returns nil if not found (e.g. not in game).
func (s *GameState) ActivePlayerFromAll() *Player {
	for i := range s.AllPlayers {
		if s.AllPlayers[i].SummonerName == s.ActivePlayer.SummonerName {
			return &s.AllPlayers[i]
		}
	}
	return nil
}
