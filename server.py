"""Websocket server that Twilio streams call audio into.

Run this once and leave it up for the whole session:

    uvicorn server:app --port 5050

Twilio opens one websocket per call against /media-stream. The scenario is passed as
a query parameter so the bridge knows which patient to be before the first frame
arrives.
"""
import traceback

from fastapi import FastAPI, WebSocket

from patient import config, persona
from patient.bridge import CallBridge

app = FastAPI(title="PGAI patient caller")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "model": config.REALTIME_MODEL}


@app.websocket("/media-stream")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()

    scenario_key = ws.query_params.get("scenario", "")
    label = ws.query_params.get("label", "call-unlabelled")
    scenario = persona.load(scenario_key)

    print(f"\n=== {label}  [{scenario.id} {scenario.label}]  voice={scenario.voice}")
    bridge = CallBridge(ws, scenario, label)
    try:
        await bridge.run()
    except Exception:
        traceback.print_exc()
    finally:
        bridge.hang_up()
        bridge.save()
        print(f"=== {label} done\n")
