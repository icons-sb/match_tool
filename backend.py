"""
backend.py — Proxy server per Funding Call Matcher
Fa da ponte tra il frontend e l'API Anthropic (CORS bypass).

Avvio:
    pip install fastapi uvicorn httpx python-dotenv
    export ANTHROPIC_API_KEY=sk-ant-...
    python backend.py

Il server gira su http://localhost:8000
"""

import os
import json
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODEL             = "claude-sonnet-4-20250514"

app = FastAPI(title="Funding Call Matcher – Backend")

# Consenti richieste dal frontend (localhost qualsiasi porta, e file://)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # In produzione restringi all'origine reale
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── Schema richiesta ─────────────────────────────────────────────────────────
class AutofillRequest(BaseModel):
    topic_id: str
    thematic_options: list[str]


# ── Endpoint principale ──────────────────────────────────────────────────────
@app.post("/api/autofill")
async def autofill(req: AutofillRequest):
    if not ANTHROPIC_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="ANTHROPIC_API_KEY non configurata sul server. "
                   "Esporta la variabile d'ambiente prima di avviare il backend."
        )

    topic_id      = req.topic_id.strip()
    thematic_list = "\n".join(f"- {t}" for t in req.thematic_options)

    prompt = f"""Cerca sul portale EU Funding & Tenders la call o il topic con identificatore: "{topic_id}"

La pagina si trova tipicamente a un URL come:
https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{topic_id}

oppure cercando "{topic_id} site:ec.europa.eu" su Google.

Dal testo della pagina estrai:
1. Il titolo completo della call/topic
2. Il "Type of action" (di solito compare come "Type of action: Research and Innovation Action" o simili)
3. Il testo completo della pagina (objective, description, scope, expected outcomes — tutto quello che trovi)

Poi, basandoti sul testo completo, classifica la call nelle seguenti aree tematiche:

{thematic_list}

Regole di classificazione:
- "primary_thematic": l'area che meglio descrive il focus principale della call
- "secondary_thematic": la seconda area più rilevante (solo se chiaramente presente nel testo, altrimenti null)
- "action_type": uno tra "RIA", "IA", "CSA", "COFUND", o null se non trovato
  - "Research and Innovation Action" → "RIA"
  - "Innovation Action" → "IA"
  - "Coordination and Support Action" → "CSA"
  - qualsiasi variante di "cofund" → "COFUND"

Rispondi SOLO con questo JSON (nessun testo prima o dopo, nessun markdown):
{{
  "title": "titolo completo",
  "action_type": "RIA" | "IA" | "CSA" | "COFUND" | null,
  "primary_thematic": "esatta stringa dall'elenco sopra" | null,
  "secondary_thematic": "esatta stringa dall'elenco sopra" | null,
  "secondary_keywords": ["keyword1", "keyword2"],
  "found": true | false
}}"""

    payload = {
        "model":      MODEL,
        "max_tokens": 1000,
        "tools":      [{"type": "web_search_20250305", "name": "web_search"}],
        "messages":   [{"role": "user", "content": prompt}],
    }

    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers)

    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Anthropic API error: {resp.text[:400]}"
        )

    data = resp.json()

    # Estrai blocchi di testo (possono essere preceduti da tool_use blocks)
    text_blocks = [
        b["text"]
        for b in data.get("content", [])
        if b.get("type") == "text"
    ]
    text = "\n".join(text_blocks).strip()

    if not text:
        raise HTTPException(status_code=502, detail="Risposta vuota dall'API Anthropic.")

    # Pulizia markdown e parsing JSON
    cleaned = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fallback: cerca il primo oggetto JSON nel testo
        import re
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            result = json.loads(match.group(0))
        else:
            raise HTTPException(
                status_code=502,
                detail=f"Impossibile parsare la risposta JSON: {cleaned[:200]}"
            )

    return result


# ── Health check ─────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "api_key_set": bool(ANTHROPIC_API_KEY),
        "model": MODEL,
    }


# ── Serve file statici (index.html, calls.json) dalla cartella corrente ──────
# Monta DOPO le route API per evitare conflitti
try:
    app.mount("/", StaticFiles(directory=".", html=True), name="static")
except Exception:
    pass  # Se la directory non contiene file statici, ignora


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    print(f"\n🚀  Backend avviato su http://localhost:{port}")
    print(f"   API key configurata: {'✓' if ANTHROPIC_API_KEY else '✗  (imposta ANTHROPIC_API_KEY!)'}\n")
    uvicorn.run("backend:app", host="0.0.0.0", port=port, reload=True)
