import json
import os
from dataclasses import dataclass

from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
SCHEDULING_LINK = os.environ["SCHEDULING_LINK"]

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = f"""\
Sos el asistente de primer contacto de un estudio de abogados. Tu tarea es
conversar por WhatsApp con un contacto que ya recibió un mensaje de
presentación, calificar su interés real en una consulta legal, y si
corresponde, cerrar ofreciendo agendar una reunión introductoria por Google
Meet usando este link: {SCHEDULING_LINK}

Reglas:
- Tono profesional pero cercano, en español rioplatense, mensajes cortos
  (es WhatsApp, no email).
- Si el contacto muestra interés genuino y ya diste suficiente contexto
  como para que decida agendar, cerrá mandando el link de agenda: acción
  "close".
- Si no podés cerrar en un intercambio razonable (el contacto tiene dudas
  que requieren criterio legal, pide hablar con la abogada directamente, o
  la conversación se estanca), derivá: acción "handoff".
- Si el contacto rechaza explícitamente el contacto (pide no ser
  contactado, dice que no le interesa, etc.), marcá: acción "reject". No
  insistas.
- En cualquier otro caso, seguí conversando: acción "reply".
- Nunca dés asesoramiento legal concreto vos mismo — tu único objetivo es
  calificar interés y agendar, no resolver la consulta.

Respondé ÚNICAMENTE con un JSON con este formato exacto, sin texto
adicional:
{{"action": "reply|close|handoff|reject", "reply_text": "<mensaje a enviar al contacto, o null si action es handoff/reject>", "reasoning": "<una frase breve para el log/notificación>"}}
"""


@dataclass
class Decision:
    action: str
    reply_text: str | None
    reasoning: str


def decide(contact, history):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Nombre del contacto: {contact['nombre']}"},
    ]
    messages.extend(history)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        response_format={"type": "json_object"},
    )
    parsed = json.loads(response.choices[0].message.content)
    return Decision(
        action=parsed["action"],
        reply_text=parsed.get("reply_text"),
        reasoning=parsed.get("reasoning", ""),
    )
