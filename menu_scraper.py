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
CALORIENINJAS_API_KEY = os.environ.get("CALORIENINJAS_API_KEY", "")

# Slovensko-anglický slovník jedál pre CalorieNinjas
SK_TO_EN_FOOD = {
    # Mäso
    "bravčov": "pork", "bravčová": "pork", "bravčové": "pork", "bravčový": "pork",
    "kuraci": "chicken", "kuracie": "chicken", "kurací": "chicken", "kurča": "chicken",
    "hovädz": "beef", "hovädzia": "beef", "hovädzie": "beef", "hovädší": "beef",
    "morčac": "turkey", "morčacia": "turkey", "morčacie": "turkey", "morčací": "turkey",
    "teľac": "veal", "telacie": "veal",
    "divinov": "venison", "divinový": "venison",
    "kačac": "duck", "kačacie": "duck",
    "losos": "salmon", "treska": "cod",
    "krevet": "shrimp", "krevety": "shrimp",
    "sekaná": "meatloaf",
    "rezeň": "schnitzel", "rezne": "schnitzel",
    "guláš": "goulash", "tokáň": "stew",
    "roštenk": "sirloin steak", "panenk": "tenderloin",
    "pečeň": "liver", "pliecko": "shoulder",
    "pulled pork": "pulled pork",
    # Prílohy
    "ryža": "rice", "ryžou": "rice", "ryži": "rice",
    "zemiaky": "potatoes", "zemiak": "potato", "zemiakov": "potato",
    "hranolk": "french fries", "hranolky": "french fries",
    "knedľ": "dumpling", "knedľa": "dumpling",
    "kaša": "mashed potatoes",
    "tarhoňa": "egg barley pasta",
    "špagety": "spaghetti", "rezance": "noodles",
    "gnocchi": "gnocchi", "rizoto": "risotto",
    # Polievky
    "polievka": "soup", "vývar": "broth", "krém": "cream soup",
    "boršč": "borscht", "minestrone": "minestrone",
    "gulášová": "goulash soup",
    # Zelenina
    "kapusta": "cabbage", "karfiol": "cauliflower",
    "špenát": "spinach", "hrášok": "peas", "hrášku": "peas",
    "fazuľ": "beans", "šošovic": "lentil",
    "cvikla": "beetroot", "mrkv": "carrot", "zelen": "vegetable",
    "šampión": "mushroom", "hubov": "mushroom", "hlivov": "oyster mushroom",
    # Jedlá
    "pizza": "pizza", "burger": "burger", "cheeseburger": "cheeseburger",
    "wrap": "wrap", "šalát": "salad", "caesar": "caesar salad",
    "vyprážan": "fried breaded", "grilovan": "grilled", "pečen": "roasted",
    "parené buchty": "steamed dumplings with jam",
    "buchty": "steamed sweet dumplings",
    "camembert": "fried camembert cheese",
    "oštiepok": "fried smoked cheese",
    "vyprážaný syr": "fried cheese",
    # Omáčky a prísady
    "tatársk": "tartar sauce", "omáčk": "sauce",
    "parmezán": "parmesan", "mozzarell": "mozzarella",
    "cheddar": "cheddar",
}


def translate_dish_to_english(dish_name: str) -> str:
    """Preloží slovenský názov jedla do angličtiny pre CalorieNinjas."""
    result_parts = []
    dish_lower = dish_name.lower()

    # Najprv skúsime dlhšie frázy
    for sk, en in sorted(SK_TO_EN_FOOD.items(), key=lambda x: -len(x[0])):
        if sk in dish_lower:
            if en not in result_parts:
                result_parts.append(en)

    if result_parts:
        return ", ".join(result_parts[:4])  # Max 4 zložky

    # Ak nič nenájdeme, vrátime originál
    return dish_name


def get_calories(dish_name: str) -> Optional[int]:
    """Získa odhad kalórií z CalorieNinjas API."""
    if not CALORIENINJAS_API_KEY:
        return None

    english_name = translate_dish_to_english(dish_name)
    log.debug("Kalórie pre: %s → %s", dish_name, english_name)

    try:
        resp = requests.get(
            "https://api.calorieninjas.com/v1/nutrition",
            params={"query": english_name},
            headers={"X-Api-Key": CALORIENINJAS_API_KEY},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("items", [])
            if items:
                total_cal = sum(item.get("calories", 0) for item in items)
                return round(total_cal) if total_cal > 0 else None
    except requests.RequestException:
        pass

    return None


def add_calories_to_menu(menu_text: str) -> str:
    """Pridá odhad kalórií k hlavným jedlám v menu."""
    if not CALORIENINJAS_API_KEY or not menu_text:
        return menu_text

    lines = menu_text.splitlines()
    result = []

    for line in lines:
        # Identifikujeme riadky s hlavnými jedlami (obsahujú A:, B:, C:, D:, E: alebo 1:, 2:, 3:, 4:)
        is_dish = bool(re.match(r'.*[A-E1-4][.:)\]]', line))
        # Preskočíme polievky, prílohy, šaláty z bufetu
        is_soup = "polievk" in line.lower() or "vývar" in line.lower()
        is_side = "príloh" in line.lower()

        if is_dish and not is_soup and not is_side:
            # Extrahujeme názov jedla (odstránime cenu, gramáž, alergény)
            dish_name = re.sub(r'\d+g[/]?\d*[g€]*', '', line)  # gramáž
            dish_name = re.sub(r'\d+[,.]?\d*€', '', dish_name)  # cena
            dish_name = re.sub(r'\d+[,]\d+€', '', dish_name)
            dish_name = re.sub(r'\(\d[\d,]*\)', '', dish_name)  # alergény v zátvorkách
            dish_name = re.sub(r'\s+\d[,\d]*\s*$', '', dish_name)  # alergény na konci
            dish_name = re.sub(r'[A-E][.:)\]]\s*', '', dish_name, count=1)
            dish_name = re.sub(r'[1-4][.:)\]]\s*', '', dish_name, count=1)
            dish_name = dish_name.strip().strip('*').strip()

            if len(dish_name) > 5:
                cal = get_calories(dish_name)
                if cal and cal > 50:
                    line = f"{line}  (~{cal} kcal)"

        result.append(line)

    return "\n".join(result)


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
    """Vyčistí text – rozdelí zlepené položky, odstráni nadbytočné medzery."""
    # 1) Vložíme nový riadok pred číslované položky (1.), 2.), A), B) atď.)
    #    ktoré sú zlepené s predchádzajúcou cenou
    text = re.sub(r'(€\s*(?:\d[\d,]*)?)\s*(\d+\.\))', r'\1\n\2', text)
    text = re.sub(r'(€\s*(?:\d[\d,]*)?)\s*([A-E]\))', r'\1\n\2', text)

    # 2) Odstránime zlepené alergény na konci ceny (8,20€1,3,7,8,9 → 8,20€)
    text = re.sub(r'([\d,]+€)\s*(\d[\d,]+)\s*$', r'\1', text, flags=re.MULTILINE)

    # 3) Zredukujeme viacnásobné medzery na jednu
    text = re.sub(r'  +', ' ', text)

    # 4) Vyčistíme riadky
    lines = [line.strip() for line in text.splitlines()]
    lines = [l for l in lines if l]
    return "\n".join(lines)


def format_tower_events(text: str) -> str:
    """Formátovanie pre Tower Events – rozdelí sekcie a položky."""
    # Rozdelíme pred sekčnými nadpismi
    text = re.sub(r'(?<!\n)(Polievka)\b', r'\n\1', text)
    text = re.sub(r'(?<!\n)(Hlavné jedlo)\b', r'\n\1', text)
    text = re.sub(r'(?<!\n)(Prílohy)\b', r'\n\1', text)

    # Rozdelíme pred položkami A: B: C: D: E: (ale len keď sú na začiatku
    # alebo po cene/medzere, nie uprostred slova)
    text = re.sub(r'(€[^\n]*?)\s*\b([A-E]:)', r'\1\n\2', text)
    # Ak sú na jednom riadku: "porcia/1,50€B: Kombi" → nový riadok
    text = re.sub(r'(€)\s*([A-E]:)', r'\1\n\2', text)

    # V A la Carte: rozdelíme pred 1. 2. 3. atď. (zlepené s €)
    text = re.sub(r'(€)\s+(\d+\.\s)', r'\1\n\2', text)

    # Zredukujeme medzery a vyčistíme
    text = re.sub(r'  +', ' ', text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def format_blue_champs(text: str) -> str:
    """Formátovanie pre Blue Champs – rozdelí polievky, fit menu a položky."""
    # Rozdelíme pred "Fit menu:" / "FIT MENU" / "Fit Menu"
    text = re.sub(r'(?<!\n)([Ff]it\s*[Mm]enu)', r'\n\1', text)

    # Rozdelíme pred objemom polievky zlepeným s predchádzajúcim textom
    # napr. "...parmezán (1,3,4,7)0,33l Šampiňónový" → nový riadok pred 0,33l
    text = re.sub(r'(\))\s*(0,\d+l\s)', r'\1\n\2', text)
    text = re.sub(r'(€)\s*(0,\d+l\s)', r'\1\n\2', text)

    # Rozdelíme pred číslovanými položkami 1: 2: 3: 4: 5:
    text = re.sub(r'(?<!\n)(?<=\))\s*(\d+:\s)', r'\n\1', text)
    text = re.sub(r'(?<!\n)(?<=€)\s*(\d+:\s)', r'\n\1', text)
    # Aj keď sú na začiatku po texte bez €/)
    text = re.sub(r'([^\n])\s+(\d+:\s+\d+g)', r'\1\n\2', text)

    # Zredukujeme medzery a vyčistíme
    text = re.sub(r'  +', ' ', text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Prilepíme osamelé číslice "1:", "2:" atď. k nasledujúcemu riadku
    merged = []
    i = 0
    while i < len(lines):
        if re.match(r'^\d+:\s*$', lines[i]) and i + 1 < len(lines):
            merged.append(lines[i].strip() + ' ' + lines[i + 1].strip())
            i += 2
        else:
            merged.append(lines[i])
            i += 1

    return "\n".join(merged)


def format_hotel_set(text: str) -> str:
    """Formátovanie pre Hotel Set – odstráni alergény, rozdelí položky, zlúči ceny."""
    # Rozdelíme zlepené položky pred č.) (1.) 2.) atď.)
    text = re.sub(r'(€\s*(?:\d[\d,]*)?)\s*(\d+\.\))', r'\1\n\2', text)
    # Odstránime alergény nalepené na konci ceny: 8,20€1,3,7,8,9
    text = re.sub(r'([\d,.]+€)\s*(\d[\d,]+)\s*$', r'\1', text, flags=re.MULTILINE)
    # Odstránime samostatné riadky s alergénmi (len čísla a čiarky)
    text = re.sub(r'^\s*\d+([,]\d+)+\s*$', '', text, flags=re.MULTILINE)
    # Odstránime alergény na konci riadku (čísla oddelené čiarkami po jednom medzere)
    text = re.sub(r'\s+(\d,)+\d\s*$', '', text, flags=re.MULTILINE)
    # Zredukujeme medzery
    text = re.sub(r'  +', ' ', text)
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Prilepíme osamelé ceny (riadok obsahujúci len cenu) k predchádzajúcemu riadku
    merged = []
    for line in lines:
        # Cena na samostatnom riadku: "8,90€" alebo "8.90€" alebo "8,90 €"
        if re.match(r'^\d+[,.]\d+\s*€$', line) and merged:
            merged[-1] = merged[-1] + ' ' + line
        else:
            merged.append(line)

    return "\n".join(merged)


def format_stage_ntc(text: str) -> str:
    """Formátovanie pre Stage NTC – zlúči osamelé ceny k jedlám."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Prilepíme osamelé ceny k predchádzajúcemu riadku
    # Cena: "8,50 €", "11,90 €", "7,90€" alebo len "8,50€"
    merged = []
    for line in lines:
        if re.match(r'^\d+[,.]\d+\s*€$', line) and merged:
            merged[-1] = merged[-1] + ' ' + line
        else:
            merged.append(line)

    return "\n".join(merged)


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

    # Hľadáme A la Carte MENU pre dnešný deň
    for row in rows:
        text = row.get_text(" ", strip=True)
        if "a la carte" in text.lower():
            # Nájdeme bunku s celým A la Carte textom
            cells = row.find_all(["td", "th"])
            for cell in cells:
                cell_text = cell.get_text(" ", strip=True)
                if len(cell_text) > 50 and "a la carte" not in cell_text.lower():
                    # Toto je bunka s jedlami pre všetky dni
                    # Rozdelíme podľa názvov dní
                    day_names_sk = {
                        0: "pondelok", 1: "utorok", 2: "streda",
                        3: "štvrtok", 4: "piatok",
                    }
                    today_name = day_names_sk.get(TODAY_INDEX, "")
                    if not today_name:
                        break

                    # Nájdeme dnešnú sekciu v texte
                    text_lower = cell_text.lower()
                    start = text_lower.find(today_name)
                    if start == -1:
                        break

                    # Posunieme sa za názov dňa a dvojbodku
                    start_content = cell_text.find(":", start) + 1
                    if start_content == 0:
                        break

                    # Nájdeme koniec (ďalší deň)
                    end = len(cell_text)
                    for d_name in day_names_sk.values():
                        if d_name == today_name:
                            continue
                        pos = text_lower.find(d_name, start_content)
                        if pos != -1 and pos < end:
                            end = pos

                    alacarte_text = cell_text[start_content:end].strip()
                    if alacarte_text:
                        lines.append("\n🍽️ *A la Carte:*")
                        lines.append(alacarte_text)
                    break
            break

    return format_tower_events("\n".join(lines))


# ===========================================================================
# 2) THE BLUE CHAMPS
# ===========================================================================

def scrape_blue_champs() -> Optional[str]:
    """
    Stránka má nadpisy pre každý deň (napr. 'Pondelok 2.3.2026').
    Používame textový prístup — extrahujeme celý text stránky
    a rozdelíme ho podľa názvov dní.
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

    # Extrahujeme celý text stránky
    full_text = soup.get_text("\n", strip=True)
    lines = full_text.splitlines()

    # Nájdeme týždennú ponuku (polievka + fit menu)
    weekly_section = ""
    all_day_names = ["pondelok", "utorok", "streda", "štvrtok", "stvrtok", "piatok"]

    for i, line in enumerate(lines):
        if "ponuka" in line.lower() and "týžd" in line.lower():
            # Zoberieme riadky až po prvý deň
            weekly_lines = []
            for j in range(i + 1, len(lines)):
                if any(d in lines[j].lower() for d in all_day_names):
                    break
                if lines[j].strip():
                    weekly_lines.append(lines[j].strip())
            weekly_section = "\n".join(weekly_lines)
            break

    # Nájdeme dnešný deň
    today_section = ""
    today_names = SK_DAYS.get(TODAY_INDEX, [])

    for i, line in enumerate(lines):
        line_lower = line.lower()
        if any(d in line_lower for d in today_names):
            # Overíme, že to vyzerá ako nadpis dňa (obsahuje dátum alebo je krátky)
            if re.search(r'\d+\.\d+\.\d{4}', line) or len(line) < 40:
                # Zoberieme riadky až po ďalší deň
                day_lines = []
                for j in range(i + 1, len(lines)):
                    next_lower = lines[j].lower()
                    # Stop ak nájdeme ďalší deň
                    is_next_day = any(
                        d in next_lower
                        for d in all_day_names
                        if d not in today_names
                    )
                    if is_next_day and re.search(r'\d+\.\d+\.\d{4}', lines[j]):
                        break
                    if lines[j].strip():
                        day_lines.append(lines[j].strip())
                today_section = "\n".join(day_lines)
                break

    if not today_section:
        log.warning("Blue Champs – dnešný deň sa nenašiel")
        return None

    result = ""
    if weekly_section:
        result += weekly_section.strip() + "\n\n"
    result += today_section.strip()

    return format_blue_champs(clean(result))


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
            # Skončíme pri texte za menu (objednávky, alergény, pôvod, váha)
            stop_keywords = [
                "nestíha", "objednáv", "tel. kontakt", "tel.kontakt",
                "šéfkuchár", "alerg", "pôvod mäsa", "používame pri",
                "váha prílohy", "objem polievky", "obilniny obsahujúce",
                "menu box", "www.hotelset",
            ]
            if any(kw in line_lower for kw in stop_keywords):
                break
            captured.append(line.strip())

    if not captured:
        log.warning("Hotel Set – dnešný deň sa nenašiel v PDF")
        return None

    return format_hotel_set("\n".join(captured))


# ===========================================================================
# 4) BRICK PUB (JPG obrázok na webe)
# ===========================================================================

def scrape_brick_pub() -> Optional[str]:
    """
    Brick Pub zverejňuje denné menu ako JPG obrázok na hlavnej stránke.
    Nájdeme URL obrázka a vrátime ho – v Slacku sa zobrazí ako odkaz.
    """
    url = "https://brickpub.sk/"
    log.info("Scrapujem Brick Pub: %s", url)

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Brick Pub – chyba pri sťahovaní: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Hľadáme obrázok denného menu v WordPress uploads
    # Typický pattern: /wp-content/uploads/2026/03/Denné-menu-*.jpg
    menu_img_url = None
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if "wp-content/uploads" in src and ("enn" in src.lower() or "menu" in src.lower()):
            # "enn" zachytí "Denné" aj "denn" (URL-encoded alebo nie)
            if src.endswith((".jpg", ".jpeg", ".png")):
                menu_img_url = src
                break

    # Skúsime aj <a> linky na obrázky
    if not menu_img_url:
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if "wp-content/uploads" in href and ("enn" in href.lower() or "menu" in href.lower()):
                if href.endswith((".jpg", ".jpeg", ".png")):
                    menu_img_url = href
                    break

    if not menu_img_url:
        log.warning("Brick Pub – nenašiel sa obrázok denného menu")
        return None

    log.info("Brick Pub – obrázok menu: %s", menu_img_url)
    return f"📷 <{menu_img_url}|Zobraziť menu (obrázok)>"


# ===========================================================================
# 5) STAGE RESTAURANT NTC (menucka.sk)
# ===========================================================================

def scrape_stage_ntc() -> Optional[str]:
    """
    Stage Restaurant NTC – menu scrapujeme z menucka.sk.
    Stránka obsahuje týždňové menu rozdelené po dňoch.
    """
    url = "https://menucka.sk/denne-menu/bratislava/restauracia-stage-ntc-n-e-w-catering"
    log.info("Scrapujem Stage NTC: %s", url)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk,cs;q=0.9,en;q=0.8",
    }

    try:
        resp = requests.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Stage NTC – chyba pri sťahovaní: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    page_text = soup.get_text(separator="\n")
    lines = [l.strip() for l in page_text.splitlines() if l.strip()]

    if TODAY_INDEX > 4:
        return None

    today_day_names = SK_DAYS.get(TODAY_INDEX, [])
    day_names_sk_full = {
        0: "Pondelok", 1: "Utorok", 2: "Streda",
        3: "Štvrtok", 4: "Piatok",
    }
    today_full = day_names_sk_full.get(TODAY_INDEX, "")

    # Nájdeme sekciu pre dnešný deň
    # Menucka.sk formát: "Pondelok (03.03.2026)" alebo "Streda (04.03.2026)"
    capturing = False
    captured = []
    today_d = date.today()
    today_date_patterns = [
        f"{today_d.day}.{today_d.month}.{today_d.year}",
        f"{today_d.day:02d}.{today_d.month:02d}.{today_d.year}",
        f"({today_d.day}.{today_d.month:02d}.{today_d.year})",
    ]

    all_day_names = []
    for v in SK_DAYS.values():
        all_day_names.extend(v)
    all_day_names_full = list(day_names_sk_full.values())

    for line in lines:
        line_lower = line.lower()

        # Začíname zachytávať pri dnešnom dni
        if not capturing:
            # Hľadáme riadok s dnešným dňom a dátumom
            if today_full.lower() in line_lower or any(dp in line for dp in today_date_patterns):
                if any(d in line_lower for d in today_day_names) or any(dp in line for dp in today_date_patterns):
                    capturing = True
                    continue

        # Skončíme pri ďalšom dni
        if capturing:
            is_next_day = False
            for day_name in all_day_names_full:
                if day_name.lower() in line_lower and day_name.lower() != today_full.lower():
                    # Overíme, že to nie je len slovo v jedle
                    if line_lower.startswith(day_name.lower()) or f"({day_name.lower()}" in line_lower:
                        is_next_day = True
                        break
            if is_next_day:
                break

            # Preskočíme irelevantné riadky
            skip_keywords = ["nenašli ste", "registrov", "zaregistrov", "menucka",
                             "jedálny lístok", "jedálne lístky", "parametre",
                             "reklam", "cookie", "appstore", "google play"]
            if any(kw in line_lower for kw in skip_keywords):
                continue

            # Zastavíme pri konci menu sekcie na menucka.sk
            stop_keywords = ["tlačiť menu", "zoznam alergénov", "zobraziť väčšiu",
                             "popis reštaurácie", "reštaurácia stage",
                             "terasa", "zobraziť ponuku", "pivovaru dock",
                             "ako motivovať", "typické jarné", "knižné novinky",
                             "všetky články", "menučka magazín", "mobilná appka",
                             "pre reštaurácie", "naše ďalšie", "sledujte nás",
                             "tipy vo vašom"]
            if any(kw in line_lower for kw in stop_keywords):
                break

            if len(line) > 2:
                captured.append(line)

    if not captured:
        log.warning("Stage NTC – dnešné menu sa nenašlo")
        return None

    return format_stage_ntc(clean("\n".join(captured)))


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
        "Brick Pub": "🧱",
        "Stage (NTC)": "🎾",
    }

    restaurant_urls = {
        "Tower Events (Cantína)": "https://towerevents.sk/menu/",
        "The Blue Champs": "https://www.thebluechamps.sk/denne-menu/",
        "Hotel Set": "https://www.hotelset.sk/domov/restauracia/",
        "Brick Pub": "https://brickpub.sk/",
        "Stage (NTC)": "https://restauraciastage.sk/",
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
        "Brick Pub": scrape_brick_pub(),
        "Stage (NTC)": scrape_stage_ntc(),
    }

    # Pridáme odhad kalórií (preskočíme Brick Pub – je to len obrázok)
    if CALORIENINJAS_API_KEY:
        log.info("Pridávam odhad kalórií (CalorieNinjas)...")
        for name in menus:
            if menus[name] and name != "Brick Pub":
                menus[name] = add_calories_to_menu(menus[name])
    else:
        log.info("CALORIENINJAS_API_KEY nie je nastavená – kalórie sa nebudú pridávať")

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
