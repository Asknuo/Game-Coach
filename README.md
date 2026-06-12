# Game Coach — LOL AI Coach Agent MVP

实时读取英雄联盟客户端数据，检测游戏事件，由 AI Agent 生成 coaching 建议，最终输出到 Overlay / Voice。

```
LOL Client → Collector (Go) → Event Engine → Agent (FastAPI) → Overlay / Voice
```

## 项目结构

```
Game Coach/
├── collector/          # Go — 数据采集 & 事件检测
│   ├── cmd/
│   ├── internal/
│   │   ├── lol/
│   │   ├── event/
│   │   └── sender/
│   └── config/
├── agent/              # Python FastAPI — 决策 & LLM
│   ├── planner/
│   ├── skills/
│   ├── memory/
│   ├── llm/
│   ├── prompt/
│   └── models/
└── docker-compose.yml  # Redis + Agent
```

## 快速开始

### 1. 启动 Redis & Agent

```bash
cp .env.example .env
# 编辑 .env 填入 OPENAI_API_KEY

docker compose up -d redis
cd agent
pip install -r requirements.txt
uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

### 2. 启动 Collector

需要正在进行的 LOL 对局（Live Client API 仅在游戏中可用）。

```bash
cd collector
go run ./cmd
```

### 3. 验证

- Agent 健康检查: http://localhost:8000/health
- Collector 连接 Agent WebSocket 后会开始推送状态与事件
- Agent 返回 coaching 建议 JSON（MVP 阶段可查看 Agent 日志）

## WebSocket 协议

**Collector → Agent**

```json
{"type": "state", "payload": { ... }}
{"type": "event", "payload": {"name": "dragon_soon", "data": { ... }}}
```

**Agent → Collector**

```json
{"type": "tip", "payload": {"message": "...", "skill": "dragon", "priority": 2}}
```

## MVP Skills

| Skill   | 触发事件           | 建议内容         |
|---------|-------------------|-----------------|
| dragon  | 龙即将刷新         | 视野 / 站位      |
| recall  | 低血量 / 危险      | 回城时机         |
| build   | 出装变化           | 针对性出装       |
| jungle  | 野区目标可用       | 入侵 / 反野      |

## 环境变量

见 `.env.example`。

## 开发说明

- Collector 通过 LoL lockfile 自动发现端口与认证信息
- 无对局时 Collector 会等待并重试
- LLM 调用失败时 Agent 会 fallback 到 skill 模板文本
