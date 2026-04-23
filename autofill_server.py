from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.sync_api import sync_playwright
import re

# Importiamo le regex dal tuo scrape_to_json.py
RE_ACTION = re.compile(r"Type of action:\s*([^\|\n\r]+)", re.IGNORECASE)
# Aggiungi qui la tua funzione classify_multitopic e TOPIC_KEYWORDS 
# copiate da scrape_to_json.py per determinare l'area secondaria

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"])

@app.get("/autofill")
def get_call_details(call_id: str):
    # Costruiamo l'URL come fa il tuo script
    url = f"https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/{call_id.lower()}"
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")
        
        # Estraiamo il contenuto testuale
        content = page.content()
        full_text = page.inner_text("body")
        title = page.title()
        
        # Estrazione Tipo di Azione
        action_match = RE_ACTION.search(full_text)
        action_type = action_match.group(1).strip() if action_match else "N/A"
        
        # Logica per Area Secondaria (esempio semplificato basato sulla tua funzione)
        # multi_data = classify_multitopic(title, full_text, call_id)
        # secondary = multi_data["multi_thematic"][1][0] if len(multi_data["multi_thematic"]) > 1 else None
        
        browser.close()
        return {
            "title": title,
            "action_type": action_type,
            "secondary_area": "Esempio Area Secondaria", # Qui inserisci il risultato di classify_multitopic
            "url": url
        }
