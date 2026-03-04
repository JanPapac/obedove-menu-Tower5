# 🍽️ Obedové menu → Slack

Automatický scraper denných menu z troch reštaurácií v okolí, s odosielaním do Slack kanála.

## Reštaurácie

| Reštaurácia | Zdroj | Metóda |
|---|---|---|
| Tower Events (Cantína) | HTML tabuľka | BeautifulSoup |
| The Blue Champs | HTML headings | BeautifulSoup |
| Hotel Set | PDF link na stránke | pypdf |

## Rýchly štart

### 1. Inštalácia závislostí

```bash
pip install -r requirements.txt
```

### 2. Vytvorenie Slack Webhooku

1. Choď na https://api.slack.com/apps → **Create New App** → **From scratch**
2. Pomenuj aplikáciu (napr. "Obedové menu") a vyber workspace
3. V ľavom menu klikni **Incoming Webhooks** → zapni **Activate**
4. Klikni **Add New Webhook to Workspace** a vyber kanál (napr. `#obedy`)
5. Skopíruj webhook URL

### 3. Nastavenie premennej prostredia

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
```

### 4. Spustenie

```bash
python menu_scraper.py
```

## Automatické spúšťanie (cron)

Pridaj do crontabu (`crontab -e`):

```cron
# Obedové menu do Slacku každý pracovný deň o 9:00
0 9 * * 1-5 cd /cesta/k/obedove_menu && SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..." /usr/bin/python3 menu_scraper.py >> /var/log/menu_scraper.log 2>&1
```

## Alternatíva: GitHub Actions

Ak nechceš riešiť vlastný server, vytvor `.github/workflows/menu.yml`:

```yaml
name: Obedové menu
on:
  schedule:
    - cron: '0 7 * * 1-5'  # 7:00 UTC = 8:00/9:00 CET/CEST
  workflow_dispatch:  # manuálne spustenie

jobs:
  send-menu:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python menu_scraper.py
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

V GitHub repo nastaveniach pridaj secret `SLACK_WEBHOOK_URL`.

## Riešenie problémov

- **Menu sa nenačítalo**: Reštaurácia mohla zmeniť štruktúru stránky. Skontroluj HTML/PDF ručne.
- **Hotel Set PDF**: URL PDF sa mení každý týždeň. Skript hľadá link s ".pdf" a "menu" v href.
- **Slack webhook nefunguje**: Over, že URL je správne a webhook je aktívny v Slack app settings.
- **Víkendy**: Skript sa automaticky preskočí cez víkend.

## Prispôsobenie

Chceš pridať ďalšiu reštauráciu? Pridaj novú funkciu `scrape_nova_restauracia()` a zaraď ju do `menus` slovníka v `main()`.
