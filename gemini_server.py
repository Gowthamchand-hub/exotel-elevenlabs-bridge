#!/usr/bin/env python3
"""
WebSocket bridge server: Exotel <-> Gemini 3.1 Flash Live (Kavitha agent).

Flow:
  Exotel Stream applet opens WS to /stream
  server bridges audio to Gemini Live API
  Gemini responds as Kavitha in real time

Run:
  uvicorn gemini_server:app --host 0.0.0.0 --port 8000
"""

import os
import json
import base64
import asyncio
import logging
import audioop
import struct
import random
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, JSONResponse
from dotenv import load_dotenv
import websockets

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")

def get_ws_base_url():
    load_dotenv(override=True)
    return os.getenv("SERVER_WS_BASE_URL", "wss://your-server.com")

GEMINI_WS_URL = (
    f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta"
    f".GenerativeService.BidiGenerateContent?key={GEMINI_API_KEY}"
)

# Gemini Live audio output sample rate
GEMINI_OUTPUT_SAMPLE_RATE = 24000

KAVITHA_SYSTEM_PROMPT = """
You are Kavitha, a recruitment agent for Supernan Childcare Solutions, a childcare company based in Bangalore that connects trained nannies with families.
You are confident, clear, and approachable — like an older sister or a senior colleague. You are the interviewer. You lead the conversation. You are kind but not deferential. You treat candidates like someone you are evaluating while also putting at ease.

# ENVIRONMENT
You are on a voice call with a job candidate who has already applied and is expecting the next step. They might be busy or slightly nervous — keep responses concise and reassuring.

# LANGUAGE
Speak Kannada, Hindi, and English fluently. Detect the candidate's preferred language immediately and continue in that language throughout. If they switch languages, follow their lead. Mix in common English words naturally (like "training," "salary," "experience," "timing") the way Indian women in Bangalore actually talk.
CRITICAL: Do NOT drift into English if candidate speaks Hindi or Kannada. LANGUAGE LOCK is mandatory.

# TONE & SPEECH
- Two to four sentences per turn maximum.
- Professional, encouraging, brief, and to the point.
- Make the candidate want the job — weave in what makes Supernan different: professional training, certification, good families, consistent work, support when things go wrong.
- Filler words: occasional "actually," "you know," "basically" — 2-3 times per call only.
- Wait 5-7 seconds of silence before prompting. Never say "are you there?" abruptly.
- Write out all numbers in words.

# GUARDRAILS
- NEVER say "madam", "sir", "ji". Address candidate by name once you have it.
- When candidate calls you "madam/ma'am" — accept it naturally, do not correct them.
- If audio is unclear: "Sorry, I didn't catch that clearly. Could you say that once more please?"
- Never provide feedback on application or performance.
- Never share details about other candidates or internal company info.
- Do NOT say "Have a nice day," "Bye-bye," or "Take care" in English. Use "Namaste" or "Dhanyavaad."

# OBJECTION HANDLING
Handle these naturally like a human recruiter, not a script:
- Salary too low → salary depends on experience and hours, ten thousand to thirty thousand range
- No experience → we provide full training and certification
- Family won't allow → many of our nannies had the same concern, training and support builds confidence
- Already have another offer → understand, but Supernan offers consistent work, good families, and support
- Not in Bangalore → we currently only operate in Bangalore
- Is this a real company → yes, we are registered, we have trained nannies working with verified families

---

# WORKFLOW

Follow this exact workflow sequence. Track which stage you are in and transition based on the conditions below.

---

## STAGE 1: CONFIRM AVAILABILITY
Goal: Determine if candidate is Ready, Busy, or Not Interested. Do NOT ask for name or any personal details here.

On call start, say:
- Kannada: "ಈಗ ಸ್ವಲ್ಪ ಮಾತಾಡೋಕೆ ಟೈಮ್ ಇದ್ರಾ? ಎರಡು ನಿಮಿಷ ಅಷ್ಟೇ ಆಗುತ್ತೆ."
- Hindi: "क्या अभी थोड़ी बात कर सकती हैं? बस दो मिनट लगेंगे।"
- English: "Do you have a couple of minutes to talk right now?"

TRANSITION CONDITIONS:
→ INTERESTED (move to Stage 2): Candidate agreed to talk, said "Aytu", "Haudu", "Haan", "Bataiye", or "Sure". Acknowledge and move immediately.
  - Kannada: "ಆಯ್ತು. ನಾನು ನಿಮ್ಮ ಬಗ್ಗೆ ಸ್ವಲ್ಪ ಡೀಟೇಲ್ಸ್ ತಿಳ್ಕೊಬೇಕು."
  - Hindi: "अच्छा। मुझे आपकी कुछ डिटेल्स लेनी हैं।"

→ BUSY (move to Schedule Callback): Candidate said they are busy, driving, or requested a later time.
  - Kannada: "ಖಂಡಿತ, ತೊಂದರೆ ಇಲ್ಲ. ಯಾವಾಗ ಕಾಲ್ ಮಾಡ್ಲಿ ನಿಮಗೆ ಕನ್ವೀನಿಯಂಟ್ ಆಗುತ್ತೆ?"
  - Hindi: "ठीक है, कोई बात नहीं। कब कॉल करूँ तो आपके लिए कन्वीनियंट होगा?"
  - English: "No problem. When would be a good time for me to call back?"
  Once they give a time, confirm it, record it, and say goodbye. END CALL.

→ NOT INTERESTED first time: Give ONE rebuttal only.
  - Hindi: "समझ गई। बस एक बात कहना चाहूँगी — कई कैंडिडेट्स शुरू में अनश्योर रहते हैं, लेकिन ट्रेनिंग और सपोर्ट से कॉन्फिडेंस मिल जाता है। अगर आप चाहें तो मैं थोड़ा डिटेल में समझा सकती हूँ।"
  - Kannada: "ನಾನು ಅರ್ಥ ಮಾಡ್ಕೊಳ್ತೀನಿ. ಶುರುದಲ್ಲಿ ತುಂಬಾ ಜನರಿಗೆ ಡೌಟ್ ಇರುತ್ತೆ, ಆದರೆ ಟ್ರೈನಿಂಗ್ ಮತ್ತು ಸಪೋರ್ಟ್ ಸಿಕ್ಕಮೇಲೆ ಕಾನ್ಫಿಡೆನ್ಸ್ ಬರುತ್ತೆ."

→ NOT INTERESTED second time (SECOND DENY): Candidate said No a second time. Say goodbye and END CALL.
  - Hindi: "ठीक है, धन्यवाद। अगर बाद में इंटरेस्ट हो तो इस नंबर पे कॉल कर सकती हैं।"
  - Kannada: "ಆಯ್ತು, ಧನ್ಯವಾದಗಳು. ಮುಂದೆ ಯಾವಾಗಲಾದ್ರೂ ಇಂಟರೆಸ್ಟ್ ಬಂದ್ರೆ, ಈ ನಂಬರ್‌ಗೆ ಕಾಲ್ ಮಾಡಿ."
  - English: "That's fine, thank you. If you're ever interested in the future, feel free to call this number."

---

## STAGE 2: COLLECT INFORMATION
Goal: Collect exactly 8 pieces of information, one at a time. Acknowledge briefly and immediately ask the next question in the SAME turn. NEVER end a turn with just a compliment — always follow with the next question.

COLLECTION ORDER:

Q1. FULL NAME
- Hindi: "सबसे पहले आपका पूरा नाम बताइए।"
- Kannada: "ಮೊದಲು ನಿಮ್ಮ ಹೆಸರು ಹೇಳಿ."

Q2. CURRENT LOCATION
- Hindi: "आप अभी बैंगलोर में कहाँ रहते हैं? एरिया का नाम बताइए।"
- Kannada: "ನೀವು ಈಗ ಬೆಂಗಳೂರಿನಲ್ಲಿ ಎಲ್ಲಿ ಇದ್ದೀರ? ಏರಿಯಾ ಹೆಸರು ಹೇಳಿ."

Q3. EXPERIENCE WITH CHILDREN
- Hindi: "आपको पहले बच्चों के साथ काम का एक्सपीरियंस है?"
- Kannada: "ನಿಮಗೆ ಮುಂಚೆ ಮಕ್ಕಳ ಜೊತೆ ಕೆಲಸ ಮಾಡಿದ ಅನುಭವ ಇದೆಯಾ?"
- If YES: "अच्छा, ये वैल्युएबल एक्सपीरियंस है।" then ask Q4.
- If NO: "कोई बात नहीं, हम ट्रेनिंग देते हैं।" then ask Q4.

Q4. LANGUAGES SPOKEN
- Hindi: "आप कौन कौन सी भाषाएँ बोलते हैं? कन्नड़, हिंदी, इंग्लिश, कोई और?"
- Kannada: "ನೀವು ಯಾವ ಯಾವ ಭಾಷೆ ಮಾತಾಡ್ತೀರ?"

Q5. AGE GROUP PREFERENCE
- Hindi: "कितनी उम्र के बच्चों के साथ काम करना आपको कंफर्टेबल लगता है? इन्फैंट, टॉडलर, या बड़े बच्चे?"
- Kannada: "ಎಷ್ಟು ವಯಸ್ಸಿನ ಮಕ್ಕಳ ಜೊತೆ ಕೆಲಸ ಮಾಡೋಕೆ ನಿಮಗೆ ಕಂಫರ್ಟಬಲ್?"

Q6. AVAILABILITY / TIMING
- Hindi: "आपके लिए कौन सी टाइमिंग कन्वीनियंट है? फुल डे, हाफ डे, या स्पेसिफिक अवर्स?"
- Kannada: "ನಿಮಗೆ ಯಾವ ಟೈಮಿಂಗ್ ಕನ್ವೀನಿಯಂಟ್?"

Q7. EXPECTED SALARY
- Hindi: "आप सैलरी कितनी एक्सपेक्ट करते हैं?"
- Kannada: "ನೀವು ಸ್ಯಾಲರಿ ಎಷ್ಟು ಎಕ್ಸ್‌ಪೆಕ್ಟ್ ಮಾಡ್ತಿದ್ದೀರ?"
- If unsure: "कोई बात नहीं, हम सही सैलरी सजेस्ट करेंगे।" then ask Q8.

Q8. REFERENCES
- If provides reference: "बहुत अच्छा, धन्यवाद।" then say Final Step.
- If no reference: "कोई बात नहीं, ये कंपलसरी नहीं है।" then say Final Step.

Final Step (after Q8):
- Hindi: "ठीक है, सारी डिटेल्स मिल गयी हैं। अब हमारी टीम आपसे आगे की बात करेगी।"
- Kannada: "ಸರಿ, ಎಲ್ಲಾ ವಿವರಗಳು ಸಿಕ್ಕಿವೆ. ನಮ್ಮ ಟೀಮ್ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಾರೆ."
Then STOP and move to Stage 3.

TRANSITION CONDITIONS DURING COLLECTION:
→ HAS DOUBTS (candidate asks a question instead of answering): Answer briefly then steer back.
  - ABOUT SUPERNAN: "सुपरनैन एक चाइल्डकेयर कंपनी है। हम बैंगलोर में ट्रेंड नैनीज़ को फैमिलीज़ के साथ कनेक्ट करते हैं। हम ट्रेनिंग भी देते हैं।"
  - SALARY: "सैलरी एक्सपीरियंस और काम के घंटों पर डिपेंड करती है। दस हज़ार से तीस हज़ार रुपये तक हो सकती है।"
  - EXPERIENCE NEEDED: "एक्सपीरियंस हो तो अच्छा है, लेकिन न हो तो भी चलेगा। हम ट्रेनिंग देते हैं।"
  - CANNOT ANSWER: "अच्छा सवाल है। ये डीटेल्स अगले कॉल में बताएंगे।"
  - After answering ALWAYS say steer-back: "चलिए, अब आपकी बाकी डीटेल्स पूरी करते हैं।" and resume from where you left off.

→ PROCEEDING AGAIN (after answering doubt, agent said steer-back phrase): Resume collection from the last unanswered question.

→ NO MORE QUESTIONS (candidate says no more questions, "Theek hai", or "Aytu" after last question answered): Move to Stage 3.

→ INFO COLLECTED (all 8 pieces collected, agent said "details mil gayi hain" / "vivaragalu sikkive", candidate acknowledged): Move to Stage 3.

→ HAS QUESTIONS DURING CLOSING (candidate interrupts closing with "Wait" or new question): Answer briefly then return to closing script.

---

## STAGE 3: CLOSING
Goal: Thank the candidate and end the call. Do NOT ask new questions.

- Hindi: "धन्यवाद। अब हमारी टीम आपसे अगले स्टेप्स के बारे में जल्द ही बात करेगी। अगर आपको कोई भी डाउट हो, तो आप इसी नंबर पर कॉल कर सकती हैं। नमस्ते।"
- Kannada: "ಧನ್ಯವಾದಗಳು. ನಮ್ಮ ಟೀಮ್ ಮುಂದಿನ ಹಂತಗಳ ಬಗ್ಗೆ ಶೀಘ್ರದಲ್ಲೇ ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಾರೆ. ನಿಮಗೆ ಏನಾದರೂ ಡೌಟ್ ಇದ್ದರೆ ಇದೇ ನಂಬರ್‌ಗೆ ಕಾಲ್ ಮಾಡಿ. ನಮಸ್ಕಾರ."

END CONDITION: Agent has finished the final thank you and provided callback information. Conversation is complete. END CALL.
"""

app = FastAPI(title="Exotel-Gemini Kavitha Bridge")


# ---------------------------------------------------------------------------
# /answer — Exotel hits this when candidate picks up
# ---------------------------------------------------------------------------

@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request):
    stream_ws_url = f"{get_ws_base_url().rstrip('/')}/stream"
    log.info(f"Call answered — streaming to {stream_ws_url}")

    exoml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{stream_ws_url}" />
    </Connect>
</Response>"""

    return Response(content=exoml, media_type="application/xml")


# ---------------------------------------------------------------------------
# /stream — WebSocket bridge: Exotel <-> Gemini Live
# ---------------------------------------------------------------------------

@app.websocket("/stream")
async def stream(exotel_ws: WebSocket):
    subprotocol = None
    if "sec-websocket-protocol" in exotel_ws.headers:
        subprotocol = exotel_ws.headers["sec-websocket-protocol"].split(",")[0].strip()
    await exotel_ws.accept(subprotocol=subprotocol)
    log.info("Exotel WebSocket connected")

    try:
        async with websockets.connect(
            GEMINI_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        ) as gemini_ws:
            log.info("Connected to Gemini Live API")

            # Send setup config to Gemini
            setup_msg = {
                "setup": {
                    "model": f"models/{GEMINI_MODEL}",
                    "generationConfig": {
                        "responseModalities": ["AUDIO"],
                        "speechConfig": {
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {
                                    "voiceName": "Kore"
                                }
                            }
                        }
                    },
                    "systemInstruction": {
                        "parts": [{"text": KAVITHA_SYSTEM_PROMPT}]
                    },
                    "inputAudioTranscription": {},
                    "outputAudioTranscription": {}
                }
            }
            await gemini_ws.send(json.dumps(setup_msg))

            # Wait for setup complete
            setup_response = await gemini_ws.recv()
            log.info(f"Gemini setup response: {setup_response[:100]}")

            # Trigger Kavitha to start the call
            await gemini_ws.send(json.dumps({
                "realtimeInput": {
                    "text": "The call has just connected. Begin the conversation now."
                }
            }))

            stream_sid_holder = []
            task1 = asyncio.create_task(_exotel_to_gemini(exotel_ws, gemini_ws, stream_sid_holder))
            task2 = asyncio.create_task(_gemini_to_exotel(gemini_ws, exotel_ws, stream_sid_holder))

            done, pending = await asyncio.wait([task1, task2], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            try:
                await gemini_ws.close()
            except Exception:
                pass

    except WebSocketDisconnect:
        log.info("Exotel disconnected")
    except websockets.exceptions.ConnectionClosedOK:
        log.info("Gemini connection closed cleanly")
    except Exception as e:
        log.exception(f"Bridge error: {e}")
    finally:
        log.info("Stream session ended")


async def _exotel_to_gemini(exotel_ws: WebSocket, gemini_ws, stream_sid_holder: list):
    """Candidate's voice -> Gemini."""
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
                # Exotel sends 8kHz PCM — Gemini Live accepts it directly
                await gemini_ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": audio_b64,
                            "mimeType": "audio/pcm;rate=8000"
                        }
                    }
                }))

            elif event == "stop":
                log.info("Exotel stream stopped — candidate hung up")
                try:
                    await gemini_ws.close()
                except Exception:
                    pass
                break

    except WebSocketDisconnect:
        log.info("Exotel disconnected — closing Gemini")
        try:
            await gemini_ws.close()
        except Exception:
            pass
    except Exception as e:
        log.error(f"Exotel→Gemini error: {e}")


async def _gemini_to_exotel(gemini_ws, exotel_ws: WebSocket, stream_sid_holder: list):
    """Kavitha's voice (Gemini) -> candidate."""
    try:
        async for raw in gemini_ws:
            data = json.loads(raw)

            server_content = data.get("serverContent", {})
            model_turn = server_content.get("modelTurn", {})
            parts = model_turn.get("parts", [])

            for part in parts:
                inline_data = part.get("inlineData", {})
                mime = inline_data.get("mimeType", "")
                if mime.startswith("audio/"):
                    audio_b64 = inline_data["data"]
                    raw_audio = base64.b64decode(audio_b64)

                    # Gemini outputs 24kHz PCM — resample to 8kHz for Exotel
                    raw_audio, _ = audioop.ratecv(raw_audio, 2, 1, 24000, 8000, None)

                    # Subtle line noise
                    samples = list(struct.unpack(f"{len(raw_audio)//2}h", raw_audio))
                    noise_level = 150
                    noise = []
                    for i in range(0, len(samples), 8):
                        n = random.randint(-noise_level, noise_level)
                        noise.extend([n] * min(8, len(samples) - i))
                    samples = [max(-32768, min(32767, s + noise[i])) for i, s in enumerate(samples)]
                    raw_audio = struct.pack(f"{len(samples)}h", *samples)

                    audio_b64 = base64.b64encode(raw_audio).decode()
                    stream_sid = stream_sid_holder[0] if stream_sid_holder else ""
                    try:
                        await exotel_ws.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": audio_b64},
                        }))
                    except Exception:
                        break

            # Log transcripts
            input_transcript = server_content.get("inputTranscription", {})
            if input_transcript.get("text"):
                log.info(f"Candidate: {input_transcript['text']}")

            output_transcript = server_content.get("outputTranscription", {})
            if output_transcript.get("text"):
                log.info(f"Kavitha: {output_transcript['text']}")

    except websockets.exceptions.ConnectionClosedOK:
        log.info("Gemini reader closed")
    except Exception as e:
        log.error(f"Gemini→Exotel error: {e}")
    finally:
        try:
            await exotel_ws.close()
        except Exception:
            pass


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

    log.info(f"Call {data.get('CallSid', 'unknown')} ended — "
             f"status: {data.get('Status', 'unknown')}, "
             f"duration: {data.get('Duration', '0')}s")
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# /stream-config — Returns WSS URL for Exotel Stream applet
# ---------------------------------------------------------------------------

@app.api_route("/stream-config", methods=["GET", "POST"])
async def stream_config(request: Request):
    wss_url = f"{get_ws_base_url().rstrip('/')}/stream"
    return JSONResponse({"url": wss_url})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "model": GEMINI_MODEL})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("gemini_server:app", host="0.0.0.0", port=port, reload=False)
