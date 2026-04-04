# Exotel → ElevenLabs Kavitha Agent — Outbound Calls

Makes outbound calls via Exotel and connects candidates to the ElevenLabs Kavitha conversational AI agent.

## Architecture

```
make_call.py
     │  POST /v1/Accounts/supernan1/Calls/connect
     ▼
 Exotel API ──── dials candidate ────► Candidate's phone
                                              │ picks up
                                              ▼
                                         GET /answer
                                         server.py
                                       returns ExoML
                                              │
                          WebSocket /stream   │
                     ◄────────────────────────┘
                     │   (mulaw 8kHz audio)
                  server.py
               (bridge / relay)
                     │   WebSocket
                     ▼
          api.elevenlabs.io/v1/convai/phone-call
               ElevenLabs Kavitha Agent
```

## Quick Start

### Step 1 — Install ngrok

```bash
brew install ngrok
```

Then get your auth token from https://dashboard.ngrok.com and add it to `.env`:
```
NGROK_AUTH_TOKEN=your_token_here
```

### Step 2 — Set up Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Step 3 — Configure credentials

```bash
cp .env.example .env
# Edit .env and fill in your credentials
```

Required variables:

| Variable | Description |
|---|---|
| `EXOTEL_API_KEY` | Exotel API key |
| `EXOTEL_API_TOKEN` | Exotel API token |
| `EXOTEL_ACCOUNT_SID` | `supernan1` |
| `EXOTEL_PHONE_NUMBER` | Your Exotel virtual number |
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `ELEVENLABS_AGENT_ID` | Kavitha agent ID |
| `NGROK_AUTH_TOKEN` | ngrok auth token |
| `TEST_CANDIDATE_NUMBER` | Default number to call |

### Step 4 — Start server + ngrok

```bash
source venv/bin/activate
bash start.sh
```

`start.sh` will:
- Start the FastAPI server on port 8000
- Start ngrok tunnel
- Auto-update `.env` with the public ngrok URLs

### Step 5 — Make a call

```bash
# Calls TEST_CANDIDATE_NUMBER from .env
python make_call.py

# Or call a specific number
python make_call.py +919876543210
```

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI server — `/answer` ExoML + `/stream` WebSocket bridge |
| `make_call.py` | CLI to initiate outbound calls via Exotel REST API |
| `start.sh` | Starts ngrok + server together, auto-updates `.env` |
| `.env.example` | Environment variable template |
| `requirements.txt` | Python dependencies |

## Troubleshooting

**`command not found: ngrok`**
```bash
brew install ngrok
```

**`401 Unauthorized` from Exotel**
- Check `EXOTEL_API_KEY` and `EXOTEL_API_TOKEN` in `.env`

**Call connects but no audio**
- Ensure `SERVER_WS_BASE_URL` uses `wss://` (not `https://`)
- Check server.py logs for WebSocket errors

**ElevenLabs connection fails**
- Verify `ELEVENLABS_API_KEY` is valid
- Confirm the agent ID is active in your ElevenLabs account

**Exotel can't reach `/answer`**
- Verify ngrok is running: `curl http://localhost:4040/api/tunnels`
- Test endpoint: `curl https://your-ngrok-url.ngrok.io/answer`
