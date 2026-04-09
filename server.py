#!/usr/bin/env python3
"""
WebSocket bridge server: Exotel <-> ElevenLabs Kavitha agent.

Flow:
  Exotel calls /answer  ->  returns ExoML with <Stream>
  Exotel opens WS to /stream  ->  bridge forwards audio to ElevenLabs
  ElevenLabs responds  ->  bridge sends audio back to Exotel (candidate hears Kavitha)

Run:
  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import os
import json
import asyncio
import logging
import audioop
import base64
import struct
import random
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")

def get_ws_base_url():
    load_dotenv(override=True)
    return os.getenv("SERVER_WS_BASE_URL", "wss://your-ngrok-url.ngrok.io")

ELEVENLABS_WS_URL = (
    f"wss://api.elevenlabs.io/v1/convai/conversation"
    f"?agent_id={ELEVENLABS_AGENT_ID}"
)

app = FastAPI(title="Exotel-ElevenLabs Bridge")

# ---------------------------------------------------------------------------
# Google Sheets setup
# ---------------------------------------------------------------------------

SHEET_ID = "11uND6NBnSpX8zy72n7UXXyMBCpAS5GrA4lAETm4Xm_Q"

def get_sheet():
    import gspread
    from google.oauth2.service_account import Credentials
    creds_json = os.getenv("GOOGLE_CREDS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDS_JSON env var not set")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).sheet1


# ---------------------------------------------------------------------------
# /answer — Exotel hits this when candidate picks up
# ---------------------------------------------------------------------------

@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    stream_ws_url = f"{get_ws_base_url().rstrip('/')}/stream"
    body = await request.body()
    log.info(f"[ANSWER] method={request.method} params={dict(request.query_params)} body={body[:500]} streaming_to={stream_ws_url}")

    exoml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream url="{stream_ws_url}" />
</Response>"""

    return Response(content=exoml, media_type="application/xml")


# ---------------------------------------------------------------------------
# /stream — WebSocket: Exotel <-> ElevenLabs audio bridge
# ---------------------------------------------------------------------------

@app.websocket("/stream")
async def stream(exotel_ws: WebSocket):
    # Accept with Exotel's expected subprotocol if provided
    subprotocol = None
    if "sec-websocket-protocol" in exotel_ws.headers:
        subprotocol = exotel_ws.headers["sec-websocket-protocol"].split(",")[0].strip()
        log.info(f"Exotel requested subprotocol: {subprotocol}")
    await exotel_ws.accept(subprotocol=subprotocol)
    log.info("Exotel WebSocket connected")

    headers = {"xi-api-key": ELEVENLABS_API_KEY}

    try:
        async with websockets.connect(
            ELEVENLABS_WS_URL,
            extra_headers=headers,
            ping_interval=20,
            ping_timeout=10,
        ) as el_ws:
            log.info(f"Connected to ElevenLabs agent: {ELEVENLABS_AGENT_ID}")

            # Send audio format config to ElevenLabs
            await el_ws.send(json.dumps({
                "type": "conversation_initiation_client_data",
                "conversation_config_override": {
                    "agent": {"agent_id": ELEVENLABS_AGENT_ID},
                    "audio": {
                        "input":  {"encoding": "linear16", "sample_rate": 16000},
                        "output": {"encoding": "linear16", "sample_rate": 16000},
                    },
                    "tts": {
                        "stability": 0.35,
                        "similarity_boost": 0.85,
                        "style": 0.60,
                        "use_speaker_boost": True,
                    },
                },
            }))

            stream_sid_holder = []
            await asyncio.gather(
                _exotel_to_elevenlabs(exotel_ws, el_ws, stream_sid_holder),
                _elevenlabs_to_exotel(el_ws, exotel_ws, stream_sid_holder),
            )

    except WebSocketDisconnect:
        log.info("Exotel disconnected")
    except websockets.exceptions.ConnectionClosedOK:
        log.info("ElevenLabs connection closed cleanly")
    except Exception as e:
        log.exception(f"Bridge error: {e}")
    finally:
        log.info("Stream session ended")


async def _exotel_to_elevenlabs(exotel_ws: WebSocket, el_ws, stream_sid_holder: list):
    """Candidate's voice -> ElevenLabs."""
    resample_state = None
    try:
        async for raw in exotel_ws.iter_text():
            data = json.loads(raw)
            event = data.get("event")

            if event == "connected":
                log.info("Exotel stream connected")

            elif event == "start":
                info = data.get("start", {})
                stream_sid = info.get("stream_sid") or info.get("streamSid", "")
                stream_sid_holder.append(stream_sid)
                log.info(f"Stream started — callSid: {info.get('call_sid')}, streamSid: {stream_sid}")

            elif event == "media":
                audio_b64 = data["media"]["payload"]
                raw = base64.b64decode(audio_b64)
                raw, resample_state = audioop.ratecv(raw, 2, 1, 8000, 16000, resample_state)
                audio_b64 = base64.b64encode(raw).decode()
                await el_ws.send(json.dumps({"user_audio_chunk": audio_b64}))

            elif event == "stop":
                log.info("Exotel stream stopped")
                break

    except WebSocketDisconnect:
        log.info("Exotel reader disconnected")
    except Exception as e:
        log.error(f"Exotel→ElevenLabs error: {e}")


async def _elevenlabs_to_exotel(el_ws, exotel_ws: WebSocket, stream_sid_holder: list):
    """Kavitha's voice -> candidate."""
    try:
        async for raw in el_ws:
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "audio":
                audio_b64 = data.get("audio_event", {}).get("audio_base_64", "")
                if audio_b64:
                    # ElevenLabs outputs 16000Hz; resample to 8000Hz for Exotel PSTN
                    raw = base64.b64decode(audio_b64)
                    raw, _ = audioop.ratecv(raw, 2, 1, 16000, 8000, None)
                    samples = list(struct.unpack(f"{len(raw)//2}h", raw))

                    # High-pass filter at 300Hz — removes bass, gives telephone "thin" sound
                    import math
                    rc = 1.0 / (2 * math.pi * 300)
                    dt = 1.0 / 8000
                    alpha = rc / (rc + dt)
                    filtered = []
                    prev_y = 0.0
                    prev_x = float(samples[0]) if samples else 0.0
                    for x in samples:
                        y = alpha * (prev_y + x - prev_x)
                        filtered.append(int(max(-32768, min(32767, y))))
                        prev_y = y
                        prev_x = x
                    samples = filtered

                    # Soft clip — simulates phone line compression
                    threshold = 26000
                    samples = [
                        int((threshold + (abs(s) - threshold) * 0.3) * (1 if s > 0 else -1))
                        if abs(s) > threshold else s
                        for s in samples
                    ]

                    # Subtle line hiss
                    noise_level = 120
                    samples = [max(-32768, min(32767, s + random.randint(-noise_level, noise_level))) for s in samples]

                    raw = struct.pack(f"{len(samples)}h", *samples)
                    audio_b64 = base64.b64encode(raw).decode()
                    stream_sid = stream_sid_holder[0] if stream_sid_holder else ""
                    try:
                        await exotel_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_b64},
                        }))
                    except Exception:
                        log.info("Exotel WS closed, stopping audio send")
                        break

            elif msg_type == "ping":
                event_id = data.get("ping_event", {}).get("event_id")
                await el_ws.send(json.dumps({"type": "pong", "event_id": event_id}))

            elif msg_type == "conversation_initiation_metadata":
                conv_id = data.get("conversation_initiation_metadata_event", {}).get("conversation_id")
                log.info(f"ElevenLabs conversation started — ID: {conv_id}")

            elif msg_type == "agent_response":
                text = data.get("agent_response_event", {}).get("agent_response", "")
                if text:
                    log.info(f"Kavitha: {text}")

            elif msg_type == "user_transcript":
                text = data.get("user_transcription_event", {}).get("user_transcript", "")
                if text:
                    log.info(f"Candidate: {text}")

            elif msg_type == "interruption":
                log.info("Interruption detected")

            elif msg_type == "error":
                log.error(f"ElevenLabs error: {data}")

    except websockets.exceptions.ConnectionClosedOK:
        log.info("ElevenLabs reader closed")
    except Exception as e:
        log.error(f"ElevenLabs→Exotel error: {e}")
    finally:
        # ElevenLabs conversation ended — close Exotel WS to hang up the call
        try:
            await exotel_ws.close()
            log.info("Hung up Exotel call after ElevenLabs conversation ended")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# /stream-config — Returns WSS URL for Exotel Stream applet (HTTPS mode)
# ---------------------------------------------------------------------------

@app.api_route("/stream-config", methods=["GET", "POST"])
async def stream_config(request: Request):
    """
    Exotel Stream applet calls this HTTPS URL and expects a JSON response
    with the WebSocket URL to connect to.
    """
    wss_url = f"{get_ws_base_url().rstrip('/')}/stream"
    log.info(f"Stream config requested — returning {wss_url}")
    return JSONResponse({"url": wss_url})


# ---------------------------------------------------------------------------
# /elevenlabs-webhook — ElevenLabs post-call webhook -> saves to Google Sheet
# ---------------------------------------------------------------------------

@app.post("/elevenlabs-webhook")
async def elevenlabs_webhook(request: Request):
    try:
        payload = await request.json()
        log.info(f"Webhook payload keys: {list(payload.keys())}")
        # ElevenLabs wraps actual data inside 'data' key
        data = payload.get("data", payload)
        log.info(f"Webhook analysis: {json.dumps(data.get('analysis', {}))}")
        log.info(f"Webhook data keys: {list(data.keys())}")
        transcript = data.get("transcript", [])
        metadata = data.get("metadata", {})
        analysis = data.get("analysis", {})

        conv_id = data.get("conversation_id", "")
        duration = metadata.get("call_duration_secs", "")

        # Extract candidate details from analysis data_collection (case-insensitive keys)
        dc = analysis.get("data_collection_results", analysis.get("data_collection", {}))
        dc_lower = {k.lower(): v for k, v in dc.items()}
        name       = dc_lower.get("candidate_name", {}).get("value", "")
        area       = dc_lower.get("candidate_area", {}).get("value", "")
        experience = dc_lower.get("candidate_experience", {}).get("value", "")
        languages  = dc_lower.get("candidate_languages", dc_lower.get("candidate_language", {})).get("value", "")
        timing     = dc_lower.get("candidate_timing", {}).get("value", "")
        salary     = dc_lower.get("candidate_salary", {}).get("value", "")

        from datetime import datetime
        date = datetime.now().strftime("%Y-%m-%d %H:%M")

        row = [date, name, area, experience, languages, timing, salary, duration, conv_id]

        sheet = get_sheet()
        sheet.append_row(row)
        log.info(f"Saved candidate to sheet: {name} | {area} | conv={conv_id}")

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)

    return Response(status_code=200)


# ---------------------------------------------------------------------------
# /status — Exotel call status callback
# ---------------------------------------------------------------------------

@app.api_route("/status", methods=["GET", "POST"])
async def status(request: Request):
    if request.method == "POST":
        form = await request.form()
        data = dict(form)
    else:
        data = dict(request.query_params)

    call_sid = data.get("CallSid", "unknown")
    call_status = data.get("Status", data.get("CallStatus", "unknown"))
    duration = data.get("Duration", "0")
    log.info(f"Call {call_sid} ended — status: {call_status}, duration: {duration}s")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "agent_id": ELEVENLABS_AGENT_ID})


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(request: Request, path: str):
    body = await request.body()
    log.info(f"[CATCH-ALL] {request.method} /{path} params={dict(request.query_params)} body={body[:500]}")
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
