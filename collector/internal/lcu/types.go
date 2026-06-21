package lcu

// Types for League Client Update (LCU) API data.

type SummonerInfo struct {
	SummonerID    int    `json:"summonerId"`
	AccountID     int    `json:"accountId"`
	DisplayName   string `json:"displayName"`
	SummonerLevel int    `json:"summonerLevel"`
	ProfileIconID int    `json:"profileIconId"`
	Puuid         string `json:"puuid"`
}

type ChampionMastery struct {
	ChampionID     int   `json:"championId"`
	ChampionLevel  int   `json:"championLevel"`
	ChampionPoints int   `json:"championPoints"`
	LastPlayTime   int64 `json:"lastPlayTime"`
	ChestGranted   bool  `json:"chestGranted"`
}

type GameFlowState struct {
	Phase   string `json:"phase"`
	GameID  int    `json:"gameId"`
	QueueID int    `json:"queueId"`
}

type RunePage struct {
	ID              int    `json:"id"`
	Name            string `json:"name"`
	PrimaryStyleID  int    `json:"primaryStyleId"`
	SubStyleID      int    `json:"subStyleId"`
	SelectedPerkIDs []int  `json:"selectedPerkIds"`
	IsActive        bool   `json:"isActive"`
}

type ChampSelectInfo struct {
	LocalPlayerCellID int           `json:"localPlayerCellId"`
	Actions           []interface{} `json:"actions"`
	MyTeam            []interface{} `json:"myTeam"`
	TheirTeam         []interface{} `json:"theirTeam"`
	Timer             float64       `json:"timer"`
	Phase             string        `json:"phase"`
}

// LCUEvent is sent to the agent WebSocket as type "event".
type LCUEvent struct {
	Name string      `json:"name"`
	Data interface{} `json:"data"`
}
