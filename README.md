# Tender radar MVP

Denní monitoring signálů, které mohou naznačovat tendr na PR/komunikační/marketingovou agenturu.

## Co to dělá

- načte RSS zdroje z `config.yaml`,
- hledá silné a slabší signály typu `tendr`, `výběrové řízení`, `PR agentura`, `nový marketingový ředitel`,
- přiřadí skóre,
- pošle ranní e-mail s prioritizovanými příležitostmi.

## Instalace na GitHubu

1. Vytvoř nový repozitář, například `tender-radar`.
2. Nahraj tyto soubory do repozitáře:
   - `radar.py`
   - `config.yaml`
   - `requirements.txt`
   - `.github/workflows/tender-radar.yml`
3. V GitHubu otevři `Settings → Secrets and variables → Actions → New repository secret`.
4. Přidej secrets níže.

## Povinné GitHub Secrets

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=odesilaci-email@example.com
SMTP_PASSWORD=heslo-nebo-app-password
MAIL_FROM=odesilaci-email@example.com
MAIL_TO=prijemce@example.com
```

U Gmailu použij App Password, ne běžné heslo.

## Test spuštění

V GitHubu otevři `Actions → Tender radar → Run workflow`.

## Lokální test bez odeslání e-mailu

```bash
pip install -r requirements.txt
DRY_RUN=true python radar.py
```

## Úpravy

V `config.yaml` můžeš měnit:

- sledované firmy,
- RSS zdroje,
- keywordy,
- scoring,
- minimální skóre pro zařazení do reportu.

## Poznámka k LinkedInu

LinkedIn není v MVP zahrnutý scrapingem. Doporučený bezpečný postup je ručně doplňovat personální změny do separátního CSV nebo později napojit placený/legální datový zdroj.
