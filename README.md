# Ivy's Base — Local Admin API

A FastAPI-powered local iMessage automation engine for managing sports picks, meal planning, happy hour scouting, and more.

## System Architecture

- **Core Engine**: `main.py` (FastAPI app with background iMessage database polling)
- **Primary Database**: macOS Chat DB (`~/Library/Messages/chat.db`)
- **Primary LLM**: DeepSeek (`deepseek-chat`); falls back to Gemini 2.5 Flash on provider failure, timeout, or empty response
- **Integrations**: Google Docs/Slides API, xAI (Grok), Odds API
- **Automation**: AppleScript for outbound iMessage routing, launchd for scheduled jobs

## Quick Start

### Setup
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Configure with your API keys
```

### Run the Engine
```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

### Available Jobs
Run jobs via iMessage, terminal, or API:
- **Sharp Picks** — Daily sports matchup analysis
- **Happy Hour Scout** — Find nearby venues and deals
- **Weekly Planner** — Generate weekly meal plans
- **Bravo Scout** — TV schedule monitoring
- **Brain** — Knowledge queries via Grok

#### Terminal Usage
```bash
./ivy list                    # See all jobs
./ivy run picks              # Run sports picks
./ivy run meals              # Run meal planner
```

#### API Usage
```bash
curl -X POST http://localhost:8000/run-job?job_name=sharp_picks \
  -H "X-API-Key: your-api-key"
```

## Project Structure

```
openclaw-admin/
├── main.py                 # Core FastAPI engine
├── config.py               # Configuration management
├── ivy_cli.py              # Command-line interface
├── job_runner.py           # Job execution system
├── agent/                  # Agent coordination
├── services/               # External service integrations
├── tools/                  # Tool registry and implementations
├── utils/                  # Utilities (AppleScript, async bridges)
├── middleware/             # Request/response middleware
├── proactive_agents/       # Background job agents
├── mcp-servers/            # MCP server implementations
├── scripts/                # Shell automation scripts
├── logs/                   # Application logs
└── .ivy/                   # Internal system files
```

## Configuration

### Environment Variables
See `.env.example` for required API keys and configuration.

### Google OAuth Setup
Copy `token.json` and `google_credentials.json` to project root:
```bash
cp ~/ai-admin-api/token.json .
cp ~/ai-admin-api/google_credentials.json .
```

## Development

### Testing Failover
```bash
export GEMINI_API_KEY="broken_test_key"
uvicorn main:app --host 127.0.0.1 --port 8000
```

### Running Tests
```bash
pytest tests/
```

### Logging
```bash
uvicorn main:app --host 127.0.0.1 --port 8000 2>&1 | tee run.log
```

## Automation Schedule

Jobs run automatically via launchd:
- **Sharp Picks**: 3x daily at 9am / 3pm / 9pm CT
- **Happy Hour Scout**: Weekly, Sundays at 12pm CST
- **Familia Meal Planner**: Sundays at 8am CST
- **Bravo Scout**: unavailable — not implemented in this repo

## Security Notes

- Endpoints require the `X-API-Key` header to match `ADMIN_SECRET`; the process fails closed and refuses to start if `ADMIN_SECRET` is unset
- Never commit `.env`, `*.pem`, or credential files
- iMessage files must be staged in `~/Pictures` (chat.db limitation)

## Troubleshooting

### Jobs Not Running
Check launchd status:
```bash
launchctl list | grep ivy
```

### iMessage Attachment Errors
Ensure files are staged under `~/Pictures` (AppleScript sandbox limitation).

### API Failures
Check logs in `logs/` directory and verify API keys in `.env`.

## Contributing

See `CLAUDE.md` for development guidelines and internal architecture.

## License

MIT
