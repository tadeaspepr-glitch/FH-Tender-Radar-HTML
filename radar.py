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
MAX_AGE_DAYS = 90

ANCHOR_CATEGORIES = {
    "direct_tender",
    "tender_result",
    "agency_change",
    "procurement",
    "people_signal",
    "business_signal",
}

CONTEXT_CATEGORIES = {"context"}


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clean_html_text(value):
    if not value:
        return ""
    value = html.unescape(str(value))
    soup = BeautifulSoup(value, "html.parser")
    return " ".join(soup.get_text(" ", strip=True).split())


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
    return parsed.netloc.replace("www.", "") or url


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
        return 20, "čerstvý článek do 3 dnů (+20)"
    if days_old <= 7:
        return 12, "čerstvý článek do 7 dnů (+12)"
    if days_old <= 30:
        return 5, "aktuální článek do 30 dnů (+5)"
    return 0, None


def contains_keyword(text, keyword):
    keyword_lower = keyword.lower().strip()
    if len(keyword_lower) <= 3:
        pattern = r"(?<!\\w)" + re.escape(keyword_lower) + r"(?!\\w)"
        return re.search(pattern, text) is not None
    return keyword_lower in text


def score_entry(title, summary, config, days_old=None):
    title_lower = title.lower()
    summary_lower = summary.lower()

    scoring = config.get("scoring", {})
    keyword_config = config.get("keywords", {})

    score = 0
    reasons = []
    anchor_found = False
    context_matches = []

    for category, keywords in keyword_config.items():
        base_points = int(scoring.get(category, 0))

        for keyword in keywords:
            in_title = contains_keyword(title_lower, keyword)
            in_summary = contains_keyword(summary_lower, keyword)

            if not in_title and not in_summary:
                continue

            is_anchor = category in ANCHOR_CATEGORIES
            is_context = category in CONTEXT_CATEGORIES

            placement = "titulek" if in_title else "perex"
            points = base_points

            if in_title:
                points = int(round(points * 1.5))

            if is_context:
                context_matches.append({
                    "label": keyword,
                    "points": points,
                    "category": category,
                    "placement": placement,
                })
                continue

            if is_anchor:
                anchor_found = True
                score += points
                reasons.append({
                    "label": keyword,
                    "points": points,
                    "category": category,
                    "placement": placement,
                    "is_anchor": True,
                })

    if anchor_found:
        for item in context_matches:
            score += item["points"]
            reasons.append({
                **item,
                "is_anchor": False,
            })

        freshness_points, freshness_reason = freshness_bonus(days_old)
        if freshness_points:
            score += freshness_points
            reasons.append({
                "label": freshness_reason,
                "points": freshness_points,
                "category": "freshness",
                "placement": "datum",
                "is_anchor": False,
            })

    return score, reasons, anchor_found


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

        pattern = r"(?<!\\w)" + re.escape(company_lower) + r"(?!\\w)"
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

        entries.append({
            "source": detected_source,
            "title": title,
            "summary": summary,
            "link": item.get("link", ""),
            "published": item.get("published", "") or item.get("updated", "") or "",
            "days_old": get_days_old(item),
        })

    return entries, warning


def collect_signals(config):
    threshold = int(config.get("threshold", 25))
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

            score, reasons, anchor_found = score_entry(
                entry["title"],
                entry["summary"],
                config,
                entry.get("days_old"),
            )

            if not anchor_found:
                continue

            if companies:
                score += 10
                reasons.append({
                    "label": "zmínka sledované firmy",
                    "points": 10,
                    "category": "company",
                    "placement": "titulek/perex",
                    "is_anchor": False,
                })

            if score >= threshold:
                all_signals.append({
                    **entry,
                    "score": score,
                    "reasons": reasons,
                    "companies": companies,
                })

    all_signals = deduplicate_signals(all_signals)
    all_signals.sort(key=lambda x: x["score"], reverse=True)

    return all_signals, notes


def deduplicate_signals(signals):
    seen = set()
    unique = []

    for signal in signals:
        normalized_title = re.sub(r"\\W+", " ", signal.get("title", "").lower()).strip()
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

    return sorted(
        company_scores.items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )[:8]


def recommendation(score):
    if score >= 120:
        return "Velmi silný signál. Ověřit tendr a okamžitě prověřit možnost účasti."
    if score >= 80:
        return "Silný signál. Zařadit do aktivního BD sledování a prověřit kontext."
    if score >= 45:
        return "Střední signál. Sledovat další vývoj a případně připravit warm intro."
    return "Slabší signál. Nechat ve sledování."


def format_reasons_text(reasons):
    if not reasons:
        return "nezjištěno"

    return ", ".join(
        f"{reason['label']} (+{reason['points']}, {reason['placement']})"
        for reason in reasons
    )


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
            age = f"{signal['days_old']} dní" if signal.get("days_old") is not None else "nezjištěno"
            reasons = format_reasons_text(signal.get("reasons", []))

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

    return "\\n".join(lines)


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
            <p>Radar běží správně. Aktuálně nenašel relevantní tendrový, agenturní, procurement nebo personální signál.</p>
        </div>
        """
    else:
        for signal in signals:
            companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
            days_old = signal.get("days_old")
            age = f"{days_old} dní" if days_old is not None else "nezjištěno"

            if days_old is None:
                age_bucket = "unknown"
            elif days_old <= 7:
                age_bucket = "week"
            elif days_old <= 30:
                age_bucket = "month"
            else:
                age_bucket = "older"

            score = signal["score"]

            if score >= 120:
                score_class = "high"
            elif score >= 80:
                score_class = "medium"
            else:
                score_class = "low"

            reasons_html = ""

            if signal.get("reasons"):
                reasons_html = "<ul class='reasons'>"
                for reason in signal["reasons"]:
                    reasons_html += (
                        f"<li>"
                        f"<strong>{html.escape(str(reason['label']))}</strong> "
                        f"<span class='points'>+{html.escape(str(reason['points']))}</span> "
                        f"<span class='placement'>{html.escape(str(reason['placement']))}</span>"
                        f"</li>"
                    )
                reasons_html += "</ul>"
            else:
                reasons_html = "<p class='muted'>Důvod nebyl zjištěn.</p>"

            cards_html += f"""
            <article class="card" data-age="{age_bucket}">
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

        .filters {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 0 0 22px 0;
        }}

        .filter-button {{
            border: 1px solid var(--border);
            background: white;
            color: var(--text);
            border-radius: 999px;
            padding: 9px 14px;
            font-weight: 700;
            cursor: pointer;
        }}

        .filter-button.active {{
            background: var(--dark);
            color: white;
            border-color: var(--dark);
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

        .card.hidden {{
            display: none;
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

        .score.high {{ background: var(--high); }}
        .score.medium {{ background: var(--medium); }}
        .score.low {{ background: var(--low); }}

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

        .points {{
            font-weight: 700;
            color: var(--dark);
        }}

        .placement {{
            color: var(--muted);
            font-size: 13px;
            margin-left: 4px;
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
            <p>Článek se zařadí pouze tehdy, pokud obsahuje skutečný tendrový, agenturní, procurement, personální nebo byznysový signál.</p>
        </section>

        <div class="filters">
            <button class="filter-button active" data-filter="all">Vše</button>
            <button class="filter-button" data-filter="week">Posledních 7 dní</button>
            <button class="filter-button" data-filter="month">Posledních 30 dní</button>
            <button class="filter-button" data-filter="older">31–90 dní</button>
        </div>

        <section class="grid" id="signals-grid">
            {cards_html}
        </section>

        {notes_html}
    </main>

    <footer>
        Generováno automaticky přes GitHub Actions. Scoring zohledňuje sílu signálu, umístění v titulku/perexu, sledované firmy a aktuálnost článku.
    </footer>

    <script>
        const buttons = document.querySelectorAll('.filter-button');
        const cards = document.querySelectorAll('.card');

        buttons.forEach(button => {{
            button.addEventListener('click', () => {{
                const filter = button.dataset.filter;

                buttons.forEach(btn => btn.classList.remove('active'));
                button.classList.add('active');

                cards.forEach(card => {{
                    const age = card.dataset.age;

                    if (filter === 'all') {{
                        card.classList.remove('hidden');
                    }} else if (filter === 'month') {{
                        if (age === 'week' || age === 'month') {{
                            card.classList.remove('hidden');
                        }} else {{
                            card.classList.add('hidden');
                        }}
                    }} else if (age === filter) {{
                        card.classList.remove('hidden');
                    }} else {{
                        card.classList.add('hidden');
                    }}
                }});
            }});
        }});
    </script>
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
