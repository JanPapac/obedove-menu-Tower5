#!/usr/bin/env python3
"""
Obedové menu scraper → Slack webhook
=====================================
Scrapuje denné menu z troch reštaurácií a posiela do Slack kanála.

Reštaurácie:
  1. Tower Events (Cantína)  — HTML tabuľka
  2. The Blue Champs         — HTML s heading per deň
  3. Hotel Set               — PDF linkovaný zo stránky

Použitie:
  pip install requests beautifulsoup4 pypdf
  export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/XXX/YYY/ZZZ"
  python menu_scraper.py
"""

import os
import re
import sys
import locale
import logging
from io import BytesIO
from datetime import datetime, date
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Konfigurácia
# ---------------------------------------------------------------------------

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Mapovanie slovenských názvov dní (bez diakritiky aj s ňou)
SK_DAYS = {
    0: ["pondelok"],
    1: ["utorok"],
    2: ["streda"],
    3: ["štvrtok", "stvrtok"],
    4: ["piatok"],
}

TODAY_INDEX = date.today().weekday()  # 0=pondelok ... 4=piatok

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def today_matches(text: str) -> bool:
    """Zistí, či text obsahuje názov dnešného dňa (slovensky)."""
    if TODAY_INDEX > 4:
        return False
    text_lower = text.lower()
    return any(day in text_lower for day in SK_DAYS[TODAY_INDEX])


def today_date_str() -> str:
    """Vráti dnešný dátum vo formáte D.M.YYYY."""
    d = date.today()
    return f"{d.day}.{d.month}.{d.year}"


def clean(text: str) -> str:
    """Vyčistí text – odstráni nadbytočné medzery a prázdne riadky."""
    lines = [line.strip() for line in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


# ===========================================================================
# 1) TOWER EVENTS (Cantína)
# ===========================================================================

def scrape_tower_events() -> Optional[str]:
    """
    Stránka obsahuje jednu veľkú HTML tabuľku s celým týždňovým menu.
    Menu je rozdelené podľa dní cez bunky s textom 'Pondelok', 'Utorok' atď.
    Extrahujeme len dnešný deň.
    """
    url = "https://towerevents.sk/menu/"
    log.info("Scrapujem Tower Events: %s", url)

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Tower Events – chyba pri sťahovaní: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        log.warning("Tower Events – nenašla sa tabuľka")
        return None

    rows = table.find_all("tr")

    # Nájdeme rozsah riadkov pre dnešný deň
    day_start = None
    day_end = None
    day_names_all = []
    for name_list in SK_DAYS.values():
        day_names_all.extend(name_list)

    for i, row in enumerate(rows):
        text = row.get_text(" ", strip=True).lower()
        # Hľadáme riadok, kde sa nachádza iba názov dňa
        if today_matches(text):
            day_start = i + 1  # nasledujúci riadok po názve dňa
        elif day_start is not None and any(d in text for d in day_names_all):
            day_end = i
            break

    if day_start is None:
        log.warning("Tower Events – dnešný deň sa nenašiel v tabuľke")
        return None

    if day_end is None:
        day_end = len(rows)

    # Extrahujeme riadky
    lines = []
    for row in rows[day_start:day_end]:
        cells = row.find_all(["td", "th"])
        cell_texts = [c.get_text(" ", strip=True) for c in cells]
        cell_texts = [t for t in cell_texts if t]
        if cell_texts:
            line = "  ".join(cell_texts)
            # Preskočíme prázdne alebo len whitespace riadky
            if line.strip():
                lines.append(line)

    if not lines:
        return None

    return "\n".join(lines)


# ===========================================================================
# 2) THE BLUE CHAMPS
# ===========================================================================

def scrape_blue_champs() -> Optional[str]:
    """
    Stránka má nadpisy <h2> pre každý deň (napr. 'Pondelok 2.3.2026')
    a pod nimi <p> tagy s jedlami.
    Taktiež je tam 'ponuka týždňa' s polievkou a fit menu.
    """
    url = "https://www.thebluechamps.sk/denne-menu/"
    log.info("Scrapujem The Blue Champs: %s", url)

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Blue Champs – chyba pri sťahovaní: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Nájdeme týždennú polievku/fit menu (sekcia "ponuka týždňa")
    weekly_section = ""
    headings = soup.find_all("h2")
    for h in headings:
        if "ponuka" in h.get_text(strip=True).lower() and "týždň" in h.get_text(strip=True).lower():
            # Zoberieme nasledujúce <p> elementy
            sibling = h.find_next_sibling()
            while sibling and sibling.name not in ["h2", "h1"]:
                txt = sibling.get_text("\n", strip=True)
                if txt:
                    weekly_section += txt + "\n"
                sibling = sibling.find_next_sibling()
            break

    # Nájdeme dnešný deň
    today_section = ""
    for h in headings:
        h_text = h.get_text(strip=True)
        if today_matches(h_text):
            sibling = h.find_next_sibling()
            while sibling and sibling.name not in ["h2", "h1"]:
                txt = sibling.get_text("\n", strip=True)
                if txt:
                    today_section += txt + "\n"
                sibling = sibling.find_next_sibling()
            break

    if not today_section:
        log.warning("Blue Champs – dnešný deň sa nenašiel")
        return None

    result = ""
    if weekly_section:
        result += weekly_section.strip() + "\n\n"
    result += today_section.strip()

    return clean(result)


# ===========================================================================
# 3) HOTEL SET (PDF)
# ===========================================================================

def scrape_hotel_set() -> Optional[str]:
    """
    Stránka reštaurácie obsahuje link na PDF s týždňovým menu.
    Najprv nájdeme URL PDF-ka, stiahneme ho a extrahujeme text.
    Potom vyfiltrujeme len dnešný deň.
    """
    page_url = "https://www.hotelset.sk/domov/restauracia/"
    log.info("Scrapujem Hotel Set: %s", page_url)

    try:
        resp = requests.get(page_url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Hotel Set – chyba pri sťahovaní stránky: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Hľadáme link na PDF (typicky obsahuje 'Denne-Menu' alebo 'denne-menu' v URL)
    pdf_url = None
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if ".pdf" in href.lower() and "menu" in href.lower():
            pdf_url = href
            break

    if not pdf_url:
        log.warning("Hotel Set – nenašiel sa link na PDF menu")
        return None

    log.info("Hotel Set – PDF URL: %s", pdf_url)

    try:
        pdf_resp = requests.get(pdf_url, timeout=15)
        pdf_resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Hotel Set – chyba pri sťahovaní PDF: %s", e)
        return None

    # Extrahujeme text z PDF
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            log.error("Hotel Set – chýba pypdf alebo PyPDF2 knižnica")
            return None

    reader = PdfReader(BytesIO(pdf_resp.content))
    full_text = ""
    for page in reader.pages:
        full_text += page.extract_text() + "\n"

    if not full_text.strip():
        log.warning("Hotel Set – PDF je prázdny alebo nečitateľný")
        return None

    # Rozdelíme text podľa dní
    day_names_all = []
    for name_list in SK_DAYS.values():
        day_names_all.extend(name_list)

    # Nájdeme dnešnú sekciu
    lines = full_text.splitlines()
    capturing = False
    captured = []

    for line in lines:
        line_lower = line.strip().lower()

        # Začíname zachytávať, keď nájdeme dnešný deň
        if today_matches(line_lower) and not capturing:
            capturing = True
            continue

        # Skončíme, keď nájdeme ďalší deň
        if capturing and any(d in line_lower for d in day_names_all if d not in SK_DAYS.get(TODAY_INDEX, [])):
            break

        if capturing and line.strip():
            # Preskočíme alergény a pôvod mäsa
            if line_lower.startswith("alerg") or "pôvod mäsa" in line_lower:
                continue
            captured.append(line.strip())

    if not captured:
        log.warning("Hotel Set – dnešný deň sa nenašiel v PDF")
        return None

    return "\n".join(captured)


# ===========================================================================
# Slack odoslanie
# ===========================================================================

def format_slack_message(menus: dict[str, Optional[str]]) -> dict:
    """Vytvorí Slack Block Kit správu z výsledkov scrapingu."""
    today = date.today()
    day_names_sk = ["Pondelok", "Utorok", "Streda", "Štvrtok", "Piatok", "Sobota", "Nedeľa"]
    day_name = day_names_sk[today.weekday()]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🍽️ Obedové menu – {day_name} {today.day}.{today.month}.{today.year}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    restaurant_emojis = {
        "Tower Events (Cantína)": "🏢",
        "The Blue Champs": "🔵",
        "Hotel Set": "🏨",
    }

    restaurant_urls = {
        "Tower Events (Cantína)": "https://towerevents.sk/menu/",
        "The Blue Champs": "https://www.thebluechamps.sk/denne-menu/",
        "Hotel Set": "https://www.hotelset.sk/domov/restauracia/",
    }

    for name, menu_text in menus.items():
        emoji = restaurant_emojis.get(name, "🍴")
        url = restaurant_urls.get(name, "")

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{emoji} <{url}|{name}>*",
                },
            }
        )

        if menu_text:
            # Slack má limit 3000 znakov na sekciu
            text = menu_text[:2900]
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": text,
                    },
                }
            )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "_Menu sa dnes nepodarilo načítať._",
                    },
                }
            )

        blocks.append({"type": "divider"})

    # Fallback text pre notifikácie
    fallback = f"Obedové menu – {day_name} {today.day}.{today.month}.{today.year}"

    return {"text": fallback, "blocks": blocks}


def send_to_slack(payload: dict) -> bool:
    """Odošle správu do Slack cez Incoming Webhook."""
    if not SLACK_WEBHOOK_URL:
        log.error("SLACK_WEBHOOK_URL nie je nastavená!")
        log.info("Nastavte: export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'")
        return False

    try:
        resp = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code == 200 and resp.text == "ok":
            log.info("Správa úspešne odoslaná do Slacku ✓")
            return True
        else:
            log.error("Slack vrátil: %s – %s", resp.status_code, resp.text)
            return False
    except requests.RequestException as e:
        log.error("Chyba pri odosielaní do Slacku: %s", e)
        return False


# ===========================================================================
# Main
# ===========================================================================

def main():
    # Víkendy preskočíme
    if TODAY_INDEX > 4:
        log.info("Dnes je víkend – preskakujem.")
        sys.exit(0)

    log.info("Spúšťam scraping menu na %s", today_date_str())

    menus = {
        "Tower Events (Cantína)": scrape_tower_events(),
        "The Blue Champs": scrape_blue_champs(),
        "Hotel Set": scrape_hotel_set(),
    }

    # Výpis do konzoly (pre debug)
    for name, text in menus.items():
        print(f"\n{'='*50}")
        print(f"  {name}")
        print(f"{'='*50}")
        if text:
            print(text)
        else:
            print("(nepodarilo sa načítať)")

    # Odoslanie do Slacku
    payload = format_slack_message(menus)

    if SLACK_WEBHOOK_URL:
        send_to_slack(payload)
    else:
        print("\n⚠️  SLACK_WEBHOOK_URL nie je nastavená – správa nebola odoslaná.")
        print("   Nastavte: export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...'")
        print("\n📋 Náhľad Slack payload (JSON):")
        import json
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
