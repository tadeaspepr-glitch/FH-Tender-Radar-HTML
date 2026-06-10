import os
import html
import yaml
import feedparser
from datetime import datetime
from urllib.parse import urlparse


CONFIG_FILE = "config.yaml"
OUTPUT_DIR = "public"
OUTPUT_FILE = "index.html"


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalise_text(value):
    if not value:
        return ""
    return " ".join(str(value).split())


def score_entry(title, summary, config):
    text = f"{title} {summary}".lower()
    score = 0
    matched_keywords = []

    scoring = config.get("scoring", {})

    for level, points in scoring.items():
        keywords = config.get("keywords", {}).get(level, [])
        for keyword in keywords:
            if keyword.lower() in text:
                score += int(points)
                matched_keywords.append(keyword)

    return score, matched_keywords


def detect_companies(title, summary, config):
    text = f"{title} {summary}".lower()
    companies = []

    for company in config.get("companies", []):
        if company.lower() in text:
            companies.append(company)

    return companies


def source_name_from_url(url):
    parsed = urlparse(url)
    host = parsed.netloc.replace("www.", "")
    return host or url


def fetch_feed(source):
    feed_url = source.get("url")
    source_name = source.get("name") or source_name_from_url(feed_url)

    parsed = feedparser.parse(feed_url)

    if parsed.bozo:
        return [], f"{source_name}: feed warning/error: {parsed.bozo_exception}"

    entries = []

    for item in parsed.entries:
        title = normalise_text(item.get("title", ""))
        summary = normalise_text(item.get("summary", ""))
        link = item.get("link", "")

        published = (
            item.get("published", "")
            or item.get("updated", "")
            or ""
        )

        entries.append({
            "source": source_name,
            "title": title,
            "summary": summary,
            "link": link,
            "published": published,
        })

    return entries, None


def collect_signals(config):
    threshold = int(config.get("threshold", 30))
    all_signals = []
    notes = []

    for source in config.get("sources", []):
        entries, warning = fetch_feed(source)

        if warning:
            notes.append(warning)

        for entry in entries:
            score, matched_keywords = score_entry(
                entry["title"],
                entry["summary"],
                config
            )

            companies = detect_companies(
                entry["title"],
                entry["summary"],
                config
            )

            if score >= threshold:
                signal = {
                    **entry,
                    "score": score,
                    "keywords": matched_keywords,
                    "companies": companies,
                }
                all_signals.append(signal)

    all_signals.sort(key=lambda x: x["score"], reverse=True)
    return all_signals, notes


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
        for i, signal in enumerate(signals, start=1):
            companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
            keywords = ", ".join(signal["keywords"]) if signal["keywords"] else "nezjištěno"

            lines.append(f"{i}. {signal['title']}")
            lines.append(f"Skóre: {signal['score']}")
            lines.append(f"Firma: {companies}")
            lines.append(f"Zdroj: {signal['source']}")
            lines.append(f"Klíčová slova: {keywords}")
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
            keywords = ", ".join(signal["keywords"]) if signal["keywords"] else "nezjištěno"

            score = signal["score"]
            if score >= 90:
                score_class = "high"
            elif score >= 60:
                score_class = "medium"
            else:
                score_class = "low"

            cards_html += f"""
            <article class="card">
                <div class="card-top">
                    <span class="score {score_class}">{score}</span>
                    <span class="source">{html.escape(signal["source"])}</span>
                </div>

                <h2>{html.escape(signal["title"])}</h2>

                <p class="summary">{html.escape(signal["summary"][:500])}</p>

                <dl>
                    <div>
                        <dt>Firma</dt>
                        <dd>{html.escape(companies)}</dd>
                    </div>
                    <div>
                        <dt>Klíčová slova</dt>
                        <dd>{html.escape(keywords)}</dd>
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
            max-width: 1100px;
            margin: 0 auto;
            padding: 24px;
        }}

        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
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

        .source {{
            color: var(--muted);
            font-size: 14px;
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
            max-width: 1100px;
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
        <section class="grid">
            {cards_html}
        </section>

        {notes_html}
    </main>

    <footer>
        Generováno automaticky přes GitHub Actions.
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
