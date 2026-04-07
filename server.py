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
                        "input":  {"encoding": "linear16", "sample_rate": 8000},
                        "output": {"encoding": "linear16", "sample_rate": 8000},
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
