import hashlib
import hmac
import json
import os

from fastapi import FastAPI, Header, Request
from starlette.concurrency import run_in_threadpool

import kapso_client
import llm
import sheets_client
import telegram
from llm import Decision  # separate name: must survive @patch("webhook.llm")

KAPSO_WEBHOOK_SECRET = os.environ["KAPSO_WEBHOOK_SECRET"]
MAX_CONTEXT_MESSAGES = 10
FALLBACK_MESSAGE = "Gracias por tu mensaje, en breve te respondemos."
LLM_FALLBACK_MESSAGE = "Perdón, tuvimos un inconveniente técnico. Te responden a la brevedad."

# Tracing is best-effort, same convention as morning-digest: if Phoenix is
# unreachable or setup fails, the webhook still has to answer messages.
try:
    from phoenix.otel import register
    from openinference.instrumentation.openai import OpenAIInstrumentor

    _tracer_provider = register(batch=False, project_name="outreach-bot")
    OpenAIInstrumentor().instrument(tracer_provider=_tracer_provider)
except Exception as e:
    print(f"Tracing no disponible, sigo sin instrumentación: {e}")

app = FastAPI()


def _verify_signature(raw_body: bytes, signature: str) -> bool:
    expected = hmac.new(KAPSO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(request: Request, x_webhook_signature: str = Header(default="")):
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_webhook_signature):
        print("Firma de webhook inválida, descarto el evento")
        return {"status": "ignored"}

    # Everything below is blocking I/O (requests/gspread/openai's sync
    # client) and can take several seconds — run it off the event loop so
    # a slow message doesn't stall /healthz and every other in-flight
    # request on this single-process server.
    return await run_in_threadpool(_process, json.loads(raw_body))


def _process(payload):
    message = payload.get("message", {})
    phone = message.get("from")
    # Kapso posts every message-related event (sent/delivered/read/failed,
    # not just received) to this same URL. Filtering on direction=="inbound"
    # covers all of them in one check: a webhook about our own outbound
    # message always carries direction="outbound" (or no "kapso" key at all
    # for non-message events like conversation.*), so this doubles as both
    # the "only new incoming messages" filter and the event-type filter.
    if not phone or message.get("kapso", {}).get("direction") != "inbound":
        return {"status": "ignored"}

    try:
        contact = sheets_client.get_contact_by_phone(phone)
    except Exception as e:
        print(f"No se pudo leer la Sheet, respondo con mensaje genérico: {e}")
        kapso_client.send_text(phone, FALLBACK_MESSAGE)
        return {"status": "ok"}

    if contact is None:
        print(f"Número no reconocido ({phone}), descarto")
        return {"status": "ignored"}

    try:
        history = kapso_client.get_history(phone, limit=MAX_CONTEXT_MESSAGES + 1)
    except Exception as e:
        print(f"No se pudo leer el historial de Kapso: {e}")
        kapso_client.send_text(phone, FALLBACK_MESSAGE)
        return {"status": "ok"}

    if len(history) > MAX_CONTEXT_MESSAGES:
        decision = Decision(
            action="handoff", reply_text=None,
            reasoning="tope de 10 mensajes de contexto alcanzado",
        )
    else:
        try:
            decision = llm.decide(contact, history)
        except Exception as e:
            print(f"LLM falló, respondo con fallback: {e}")
            kapso_client.send_text(phone, LLM_FALLBACK_MESSAGE)
            return {"status": "ok"}

    _dispatch(contact, phone, decision)
    return {"status": "ok"}


def _dispatch(contact, phone, decision):
    try:
        if decision.action == "reply":
            kapso_client.send_text(phone, decision.reply_text)
            sheets_client.update_status(contact["row_number"], "in_conversation")
        elif decision.action == "close":
            kapso_client.send_text(phone, decision.reply_text)
            sheets_client.update_status(contact["row_number"], "qualified")
            telegram.send_telegram(f"✅ Lead calificado: {contact['nombre']} ({phone})\n{decision.reasoning}")
        elif decision.action == "handoff":
            sheets_client.update_status(contact["row_number"], "handoff")
            telegram.send_telegram(f"🤝 Derivar a humano: {contact['nombre']} ({phone})\n{decision.reasoning}")
        elif decision.action == "reject":
            sheets_client.update_status(contact["row_number"], "not_interested")
    except Exception as e:
        print(f"Fallo despachando la decisión ({decision.action}) para {phone}: {e}")
