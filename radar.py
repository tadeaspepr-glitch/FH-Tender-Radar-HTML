import os
import re
import html
import time
import yaml
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urlparse


CONFIG_FILE = "config.yaml"
OUTPUT_DIR = "public"
OUTPUT_FILE = "index.html"
MAX_AGE_DAYS = 45


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_html_text(value):
    if not value:
        return ""

    value = html.unescape(str(value))
    soup = BeautifulSoup(value, "html.parser")
    text = soup.get_text(" ", strip=True)

    return " ".join(text.split())


def extract_google_news_title_and_source(raw_title, raw_summary, default_source):
    title = clean_html_text(raw_title)
    summary = str(raw_summary or "")
    source = default_source

    if "<a " in summary.lower():
        soup = BeautifulSoup(summary, "html.parser")
        first_link = soup.find("a")

        if first_link:
            linked_title = first_link.get_text(" ", strip=True)
            if linked_title:
                title = clean_html_text(linked_title)

        font_tag = soup.find("font")
        if font_tag:
            extracted_source = font_tag.get_text(" ", strip=True)
            if extracted_source:
                source = clean_html_text(extracted_source)

    if " - " in title and default_source.lower().startswith("google news"):
        possible_title, possible_source = title.rsplit(" - ", 1)
        if len(possible_source) < 50:
            title = possible_title.strip()
            source = possible_source.strip()

    return title, source


def source_name_from_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    return host or url


def get_entry_datetime(item):
    parsed_date = item.get("published_parsed") or item.get("updated_parsed")

    if not parsed_date:
        return None

    try:
        return datetime.fromtimestamp(time.mktime(parsed_date))
    except Exception:
        return None


def get_days_old(item):
    published_dt = get_entry_datetime(item)

    if not published_dt:
        return None

    return max(0, (datetime.now() - published_dt).days)


def is_recent(item):
    published_dt = get_entry_datetime(item)

    if not published_dt:
        return True

    return published_dt >= datetime.now() - timedelta(days=MAX_AGE_DAYS)


def freshness_bonus(days_old):
    if days_old is None:
        return 0, None

    if days_old <= 3:
        return 30, f"čerstvý článek do 3 dnů (+30)"
    if days_old <= 7:
        return 20, f"čerstvý článek do 7 dnů (+20)"
    if days_old <= 30:
        return 10, f"aktuální článek do 30 dnů (+10)"

    return 0, None


def score_entry(title, summary, config, days_old=None):
    text = f"{title} {summary}".lower()
    score = 0
    keyword_reasons = []

    scoring = config.get("scoring", {})

    for level, points in scoring.items():
        keywords = config.get("keywords", {}).get(level, [])
        for keyword in keywords:
            if keyword.lower() in text:
                score += int(points)
                keyword_reasons.append({
                    "label": keyword,
                    "points": int(points),
                    "type": level,
                })

    freshness_points, freshness_reason = freshness_bonus(days_old)
    score += freshness_points

    reasons = []

    for reason in keyword_reasons:
        reasons.append(f"{reason['label']} (+{reason['points']})")

    if freshness_reason:
        reasons.append(freshness_reason)

    return score, keyword_reasons, freshness_points, reasons


def detect_companies(title, summary, config, source):
    text = f"{title} {summary}".lower()
    companies = []

    ignored_company_names = {
        "google",
        "google news",
        "mediář",
        "mediar",
        "mam",
        "mediaguru",
        "marketing journal",
        "czechcrunch",
        "lupa",
    }

    source_lower = (source or "").lower()

    for company in config.get("companies", []):
        company_lower = company.lower()

        if company_lower in ignored_company_names:
            continue

        if company_lower == source_lower:
            continue

        pattern = r"(?<!\w)" + re.escape(company_lower) + r"(?!\w)"

        if re.search(pattern, text):
            companies.append(company)

    return companies


def fetch_feed(source):
    feed_url = source.get("url")
    configured_source_name = source.get("name") or source_name_from_url(feed_url)

    parsed = feedparser.parse(feed_url)

    entries = []
    warning = None

    if parsed.bozo:
        warning = f"{configured_source_name}: feed warning/error: {parsed.bozo_exception}"

    for item in parsed.entries:
        if not is_recent(item):
            continue

        raw_title = item.get("title", "")
        raw_summary = item.get("summary", "")

        title, detected_source = extract_google_news_title_and_source(
            raw_title,
            raw_summary,
            configured_source_name
        )

        summary = clean_html_text(raw_summary)
        link = item.get("link", "")

        published = (
            item.get("published", "")
            or item.get("updated", "")
            or ""
        )

        entries.append({
            "source": detected_source,
            "title": title,
            "summary": summary,
            "link": link,
            "published": published,
            "days_old": get_days_old(item),
        })

    return entries, warning


def collect_signals(config):
    threshold = int(config.get("threshold", 30))
    all_signals = []
    notes = []

    for source in config.get("sources", []):
        entries, warning = fetch_feed(source)

        if warning:
            notes.append(warning)

        for entry in entries:
            companies = detect_companies(
                entry["title"],
                entry["summary"],
                config,
                entry["source"],
            )

            score, keyword_reasons, freshness, reasons = score_entry(
                entry["title"],
                entry["summary"],
                config,
                entry.get("days_old"),
            )

            # Záznam neprojde jen díky stáří článku.
            # Musí mít buď keyword match, nebo zmínku sledované firmy.
            if not keyword_reasons and not companies:
                continue

            # Pokud má jen firmu, ale žádný keyword, držíme ho níže.
            if companies and not keyword_reasons:
                score += 5
                reasons.append("zmínka sledované firmy (+5)")

            if score >= threshold:
                signal = {
                    **entry,
                    "score": score,
                    "keyword_reasons": keyword_reasons,
                    "freshness": freshness,
                    "reasons": reasons,
                    "companies": companies,
                }
                all_signals.append(signal)

    all_signals = deduplicate_signals(all_signals)
    all_signals.sort(key=lambda x: x["score"], reverse=True)

    return all_signals, notes


def deduplicate_signals(signals):
    seen = set()
    unique = []

    for signal in signals:
        normalized_title = re.sub(
            r"\W+",
            " ",
            signal.get("title", "").lower()
        ).strip()

        key = normalized_title[:120]

        if key in seen:
            continue

        seen.add(key)
        unique.append(signal)

    return unique


def build_company_summary(signals):
    company_scores = {}

    for signal in signals:
        for company in signal.get("companies", []):
            if company not in company_scores:
                company_scores[company] = {
                    "score": 0,
                    "count": 0,
                    "top_signal": "",
                }

            company_scores[company]["score"] += signal.get("score", 0)
            company_scores[company]["count"] += 1

            if not company_scores[company]["top_signal"]:
                company_scores[company]["top_signal"] = signal.get("title", "")

    sorted_companies = sorted(
        company_scores.items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )

    return sorted_companies[:8]


def recommendation(score):
    if score >= 90:
        return "Ověřit možnost účasti v tendru / kontaktovat relevantní decision makery."
    if score >= 60:
        return "Zařadit do aktivního BD sledování a prověřit kontext."
    if score >= 30:
        return "Sledovat další vývoj a případné navazující signály."
    return "Nízká priorita."


def build_text_report(signals, notes):
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"Tender radar – {today}", ""]

    if not signals:
        lines.append("Dnes nebyly nalezeny žádné signály nad nastaveným prahem.")
    else:
        top_companies = build_company_summary(signals)

        if top_companies:
            lines.append("Top firmy podle skóre:")
            for company, data in top_companies:
                lines.append(f"- {company}: {data['score']} bodů / {data['count']} signálů")
            lines.append("")

        for i, signal in enumerate(signals, start=1):
            companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
            reasons = ", ".join(signal["reasons"]) if signal["reasons"] else "nezjištěno"
            age = f"{signal['days_old']} dní" if signal.get("days_old") is not None else "nezjištěno"

            lines.append(f"{i}. {signal['title']}")
            lines.append(f"Skóre: {signal['score']}")
            lines.append(f"Firma: {companies}")
            lines.append(f"Zdroj: {signal['source']}")
            lines.append(f"Stáří: {age}")
            lines.append(f"Důvody zařazení: {reasons}")
            lines.append(f"Doporučení: {recommendation(signal['score'])}")
            lines.append(f"Odkaz: {signal['link']}")
            lines.append("")

    if notes:
        lines.append("Poznámky ke zdrojům:")
        for note in notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


def build_html_report(signals, notes):
    today = datetime.now().strftime("%d.%m.%Y")
    top_companies = build_company_summary(signals)

    summary_html = ""

    if top_companies:
        company_cards = ""

        for company, data in top_companies:
            company_cards += f"""
            <div class="company-card">
                <div class="company-name">{html.escape(company)}</div>
                <div class="company-score">{data["score"]}</div>
                <div class="company-meta">{data["count"]} signálů</div>
                <div class="company-signal">{html.escape(data["top_signal"][:120])}</div>
            </div>
            """

        summary_html = f"""
        <section class="summary-panel">
            <div class="section-heading">
                <h2>Top firmy podle skóre</h2>
                <p>Součet bodů ze všech nalezených signálů za posledních {MAX_AGE_DAYS} dní.</p>
            </div>
            <div class="company-grid">
                {company_cards}
            </div>
        </section>
        """

    cards_html = ""

    if not signals:
        cards_html = """
        <div class="empty">
            <h2>Dnes nebyly nalezeny žádné signály nad nastaveným prahem.</h2>
            <p>Radar běží správně, jen aktuálně nenašel relevantní zmínky.</p>
        </div>
        """
    else:
        for signal in signals:
            companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
            age = f"{signal['days_old']} dní" if signal.get("days_old") is not None else "nezjištěno"

            score = signal["score"]

            if score >= 90:
                score_class = "high"
            elif score >= 60:
                score_class = "medium"
            else:
                score_class = "low"

            reasons_html = ""

            if signal.get("reasons"):
                reasons_html = "<ul class='reasons'>"
                for reason in signal["reasons"]:
                    reasons_html += f"<li>{html.escape(reason)}</li>"
                reasons_html += "</ul>"
            else:
                reasons_html = "<p class='muted'>Důvod nebyl zjištěn.</p>"

            cards_html += f"""
            <article class="card">
                <div class="card-top">
                    <span class="score {score_class}">{score}</span>
                    <div class="meta">
                        <span class="source">{html.escape(signal["source"])}</span>
                        <span class="age">{html.escape(age)}</span>
                    </div>
                </div>

                <h2>{html.escape(signal["title"])}</h2>

                <p class="summary">{html.escape(signal["summary"][:420])}</p>

                <dl>
                    <div>
                        <dt>Firma</dt>
                        <dd>{html.escape(companies)}</dd>
                    </div>
                    <div>
                        <dt>Důvody zařazení</dt>
                        <dd>{reasons_html}</dd>
                    </div>
                    <div>
                        <dt>Doporučení</dt>
                        <dd>{html.escape(recommendation(score))}</dd>
                    </div>
                </dl>

                <a class="button" href="{html.escape(signal["link"])}" target="_blank" rel="noopener noreferrer">
                    Otevřít zdroj
                </a>
            </article>
            """

    notes_html = ""

    if notes:
        notes_html = "<section class='notes'><h2>Poznámky ke zdrojům</h2><ul>"
        for note in notes:
            notes_html += f"<li>{html.escape(note)}</li>"
        notes_html += "</ul></section>"

    return f"""<!doctype html>
<html lang="cs">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Tender radar – {today}</title>
    <style>
        :root {{
            --bg: #f5f6fa;
            --card: #ffffff;
            --text: #151922;
            --muted: #687082;
            --border: #e4e7ee;
            --high: #b42318;
            --medium: #b76e00;
            --low: #2563eb;
            --dark: #111827;
            --soft: #eef2ff;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            background: var(--bg);
            color: var(--text);
        }}

        header {{
            background: var(--dark);
            color: white;
            padding: 32px 24px;
        }}

        header h1 {{
            margin: 0 0 8px 0;
            font-size: 32px;
        }}

        header p {{
            margin: 0;
            color: #d1d5db;
        }}

        main {{
            max-width: 1180px;
            margin: 0 auto;
            padding: 24px;
        }}

        .section-heading {{
            margin-bottom: 16px;
        }}

        .section-heading h2 {{
            margin: 0 0 4px 0;
        }}

        .section-heading p {{
            margin: 0;
            color: var(--muted);
        }}

        .summary-panel {{
            margin-bottom: 28px;
        }}

        .company-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
        }}

        .company-card {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 18px;
            box-shadow: 0 8px 22px rgba(15, 23, 42, 0.05);
        }}

        .company-name {{
            font-weight: 800;
            font-size: 18px;
            margin-bottom: 10px;
        }}

        .company-score {{
            font-weight: 800;
            font-size: 34px;
            line-height: 1;
            margin-bottom: 4px;
        }}

        .company-meta {{
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 10px;
        }}

        .company-signal {{
            color: var(--muted);
            font-size: 13px;
            line-height: 1.35;
        }}

        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(330px, 1fr));
            gap: 20px;
        }}

        .card, .empty, .notes {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 22px;
            box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
        }}

        .card-top {{
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            margin-bottom: 14px;
        }}

        .score {{
            display: inline-flex;
            width: 48px;
            height: 48px;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            color: white;
            font-weight: 700;
            font-size: 18px;
            flex: 0 0 auto;
        }}

        .score.high {{
            background: var(--high);
        }}

        .score.medium {{
            background: var(--medium);
        }}

        .score.low {{
            background: var(--low);
        }}

        .meta {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
            gap: 4px;
            min-width: 0;
        }}

        .source {{
            color: var(--muted);
            font-size: 14px;
            text-align: right;
            font-weight: 700;
        }}

        .age {{
            color: var(--muted);
            font-size: 13px;
            text-align: right;
        }}

        h2 {{
            margin: 0 0 12px 0;
            line-height: 1.25;
            font-size: 21px;
        }}

        .summary {{
            color: var(--muted);
            line-height: 1.5;
            margin-bottom: 18px;
        }}

        .muted {{
            color: var(--muted);
        }}

        dl {{
            margin: 0 0 18px 0;
        }}

        dl div {{
            border-top: 1px solid var(--border);
            padding: 10px 0;
        }}

        dt {{
            font-weight: 700;
            font-size: 13px;
            color: var(--muted);
            text-transform: uppercase;
            letter-spacing: .04em;
            margin-bottom: 4px;
        }}

        dd {{
            margin: 0;
            line-height: 1.45;
        }}

        .reasons {{
            margin: 0;
            padding-left: 18px;
        }}

        .reasons li {{
            margin: 4px 0;
        }}

        .button {{
            display: inline-block;
            background: var(--dark);
            color: white;
            text-decoration: none;
            padding: 10px 14px;
            border-radius: 10px;
            font-weight: 700;
        }}

        .notes {{
            margin-top: 24px;
        }}

        .notes ul {{
            margin-bottom: 0;
        }}

        footer {{
            max-width: 1180px;
            margin: 0 auto;
            padding: 0 24px 32px 24px;
            color: var(--muted);
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <header>
        <h1>Tender radar</h1>
        <p>Automatický monitoring signálů k PR, marketingovým a komunikačním tendrům · aktualizováno {today}</p>
    </header>

    <main>
        {summary_html}

        <section class="section-heading">
            <h2>Nalezené signály</h2>
            <p>Zahrnuty jsou pouze články za posledních {MAX_AGE_DAYS} dní, které obsahují relevantní keyword nebo sledovanou firmu.</p>
        </section>

        <section class="grid">
            {cards_html}
        </section>

        {notes_html}
    </main>

    <footer>
        Generováno automaticky přes GitHub Actions. Scoring kombinuje klíčová slova, sledované firmy a aktuálnost článku.
    </footer>
</body>
</html>
"""


def save_html(html_body):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_body)

    return output_path


def main():
    config = load_config()
    signals, notes = collect_signals(config)

    text_body = build_text_report(signals, notes)
    html_body = build_html_report(signals, notes)

    output_path = save_html(html_body)

    print(text_body)
    print("")
    print(f"HTML report saved to: {output_path}")


if __name__ == "__main__":
    main()
