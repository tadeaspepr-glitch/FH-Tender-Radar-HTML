import os
import re
import html
import time
import yaml
import smtplib
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from urllib.parse import urlparse
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


CONFIG_FILE = "config.yaml"
OUTPUT_DIR = "public"
OUTPUT_FILE = "index.html"
MAX_AGE_DAYS = 90
EMAIL_TOP_LIMIT = 5

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
        return 20, "čerstvý článek do 3 dnů"
    if days_old <= 7:
        return 12, "čerstvý článek do 7 dnů"
    if days_old <= 30:
        return 5, "aktuální článek do 30 dnů"

    return 0, None


def contains_keyword(text, keyword):
    keyword_lower = keyword.lower().strip()

    if len(keyword_lower) <= 3:
        pattern = r"(?<!\w)" + re.escape(keyword_lower) + r"(?!\w)"
        return re.search(pattern, text) is not None

    return keyword_lower in text


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

            placement = "titulek" if in_title else "perex"
            points = base_points

            if in_title:
                points = int(round(points * 1.5))

            is_anchor = category in ANCHOR_CATEGORIES
            is_context = category in CONTEXT_CATEGORIES

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

    return score, reasons, anchor_found, context_matches


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
    threshold = int(config.get("threshold", 20))
    strong_signals = []
    fallback_signals = []
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

            score, reasons, anchor_found, context_matches = score_entry(
                entry["title"],
                entry["summary"],
                config,
                entry.get("days_old"),
            )

            if companies:
                score += 10
                reasons.append({
                    "label": "zmínka sledované firmy",
                    "points": 10,
                    "category": "company",
                    "placement": "titulek/perex",
                    "is_anchor": False,
                })

            signal = {
                **entry,
                "score": score,
                "reasons": reasons,
                "companies": companies,
                "is_fallback": False,
            }

            if anchor_found and score >= threshold:
                strong_signals.append(signal)
                continue

            if companies and (score >= 10 or context_matches):
                fallback_signal = {
                    **signal,
                    "is_fallback": True,
                }

                if not fallback_signal["reasons"]:
                    fallback_signal["reasons"] = [{
                        "label": "slabší zmínka sledované firmy",
                        "points": 10,
                        "category": "fallback",
                        "placement": "titulek/perex",
                        "is_anchor": False,
                    }]

                fallback_signals.append(fallback_signal)

    strong_signals = deduplicate_signals(strong_signals)
    fallback_signals = deduplicate_signals(fallback_signals)

    strong_signals.sort(key=lambda x: x["score"], reverse=True)
    fallback_signals.sort(key=lambda x: x["score"], reverse=True)

    return strong_signals, fallback_signals[:24], notes


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

    return sorted(
        company_scores.items(),
        key=lambda item: item[1]["score"],
        reverse=True
    )[:8]


def recommendation(score, is_fallback=False):
    if is_fallback:
        return "Slabší signál. Ověřit ručně, zda souvisí s komunikací, změnou agentury nebo možnou příležitostí."

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


def build_text_report(strong_signals, fallback_signals, notes):
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"Tender radar – {today}", ""]

    if not strong_signals and not fallback_signals:
        lines.append("Dnes nebyly nalezeny žádné signály.")
    else:
        if strong_signals:
            lines.append("Silné signály:")
            for i, signal in enumerate(strong_signals, start=1):
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

        if fallback_signals:
            lines.append("Slabší signály k ověření:")
            for i, signal in enumerate(fallback_signals, start=1):
                companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
                age = f"{signal['days_old']} dní" if signal.get("days_old") is not None else "nezjištěno"
                reasons = format_reasons_text(signal.get("reasons", []))

                lines.append(f"{i}. {signal['title']}")
                lines.append(f"Skóre: {signal['score']}")
                lines.append(f"Firma: {companies}")
                lines.append(f"Zdroj: {signal['source']}")
                lines.append(f"Stáří: {age}")
                lines.append(f"Důvody zařazení: {reasons}")
                lines.append(f"Odkaz: {signal['link']}")
                lines.append("")

    if notes:
        lines.append("Poznámky ke zdrojům:")
        for note in notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


def age_bucket(days_old):
    if days_old is None:
        return "unknown"
    if days_old <= 7:
        return "week"
    if days_old <= 30:
        return "month"
    return "older"


def render_cards(signals):
    cards_html = ""

    for signal in signals:
        companies = ", ".join(signal["companies"]) if signal["companies"] else "nezjištěno"
        days_old = signal.get("days_old")
        age = f"{days_old} dní" if days_old is not None else "nezjištěno"
        bucket = age_bucket(days_old)

        score = signal["score"]

        if signal.get("is_fallback"):
            score_class = "fallback"
        elif score >= 120:
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

        fallback_badge = ""
        if signal.get("is_fallback"):
            fallback_badge = "<span class='badge'>slabší signál</span>"

        cards_html += f"""
        <article class="card" data-age="{bucket}">
            <div class="card-top">
                <span class="score {score_class}">{score}</span>
                <div class="meta">
                    <span class="source">{html.escape(signal["source"])}</span>
                    <span class="age">{html.escape(age)}</span>
                    {fallback_badge}
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
                    <dd>{html.escape(recommendation(score, signal.get("is_fallback", False)))}</dd>
                </div>
            </dl>

            <a class="button" href="{html.escape(signal["link"])}" target="_blank" rel="noopener noreferrer">
                Otevřít zdroj
            </a>
        </article>
        """

    return cards_html


def build_html_report(strong_signals, fallback_signals, notes):
    today = datetime.now().strftime("%d.%m.%Y")
    all_signals = strong_signals + fallback_signals
    top_companies = build_company_summary(all_signals)

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

    strong_cards = render_cards(strong_signals)
    fallback_cards = render_cards(fallback_signals)

    if not strong_cards:
        strong_cards = """
        <div class="empty">
            <h2>Dnes nebyly nalezeny žádné silné signály.</h2>
            <p>Podívej se níže na slabší signály k ověření. Ty pomáhají ladit keywordy a sledované firmy.</p>
        </div>
        """

    fallback_section = ""
    if fallback_signals:
        fallback_section = f"""
        <section class="section-heading secondary-heading">
            <h2>Slabší signály k ověření</h2>
            <p>Fallback režim: položky se sledovanou firmou nebo slabším kontextem. Neznamenají tendr, ale pomáhají ladit záběr.</p>
        </section>

        <section class="grid">
            {fallback_cards}
        </section>
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
            --fallback: #64748b;
            --dark: #111827;
        }}

        * {{ box-sizing: border-box; }}

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

        .secondary-heading {{
            margin-top: 34px;
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
        .score.fallback {{ background: var(--fallback); }}

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

        .badge {{
            background: #eef2f7;
            color: #475569;
            font-size: 12px;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 999px;
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

        .muted {{ color: var(--muted); }}

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
            <h2>Silné signály</h2>
            <p>Články s tendrovým, agenturním, procurement, personálním nebo byznysovým signálem.</p>
        </section>

        <div class="filters">
            <button class="filter-button active" data-filter="all">Vše</button>
            <button class="filter-button" data-filter="week">Posledních 7 dní</button>
            <button class="filter-button" data-filter="month">Posledních 30 dní</button>
            <button class="filter-button" data-filter="older">31–90 dní</button>
        </div>

        <section class="grid">
            {strong_cards}
        </section>

        {fallback_section}

        {notes_html}
    </main>

    <footer>
        Generováno automaticky přes GitHub Actions. Fallback režim slouží k ladění zdrojů a keywordů.
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


def get_dashboard_url():
    return os.environ.get("DASHBOARD_URL", "").strip()


def get_top_email_signals(strong_signals, fallback_signals):
    combined = strong_signals + fallback_signals
    combined.sort(key=lambda x: x.get("score", 0), reverse=True)
    return combined[:EMAIL_TOP_LIMIT]


def build_email_bodies(strong_signals, fallback_signals):
    today = datetime.now().strftime("%d.%m.%Y")
    top_signals = get_top_email_signals(strong_signals, fallback_signals)
    dashboard_url = get_dashboard_url()

    total_strong = len(strong_signals)
    total_fallback = len(fallback_signals)

    text_lines = [
        f"Tender radar – {today}",
        "",
        f"Silné signály: {total_strong}",
        f"Slabší signály k ověření: {total_fallback}",
        "",
        f"TOP {EMAIL_TOP_LIMIT} signálů:",
        "",
    ]

    if not top_signals:
        text_lines.append("Dnes nebyly nalezeny žádné signály.")
    else:
        for index, signal in enumerate(top_signals, start=1):
            companies = ", ".join(signal.get("companies", [])) or "nezjištěno"
            age = f"{signal.get('days_old')} dní" if signal.get("days_old") is not None else "nezjištěno"
            reasons = format_reasons_text(signal.get("reasons", []))

            text_lines.extend([
                f"{index}. {signal.get('title', '')}",
                f"Skóre: {signal.get('score', 0)}",
                f"Firma: {companies}",
                f"Zdroj: {signal.get('source', '')}",
                f"Stáří: {age}",
                f"Důvody: {reasons}",
                f"Odkaz: {signal.get('link', '')}",
                "",
            ])

    if dashboard_url:
        text_lines.extend([
            "Dashboard:",
            dashboard_url,
            "",
        ])

    text_body = "\n".join(text_lines)

    signal_items_html = ""

    if not top_signals:
        signal_items_html = "<p>Dnes nebyly nalezeny žádné signály.</p>"
    else:
        for index, signal in enumerate(top_signals, start=1):
            companies = ", ".join(signal.get("companies", [])) or "nezjištěno"
            age = f"{signal.get('days_old')} dní" if signal.get("days_old") is not None else "nezjištěno"
            reasons = format_reasons_text(signal.get("reasons", []))
            badge = "Slabší signál" if signal.get("is_fallback") else "Silný signál"

            signal_items_html += f"""
            <div style="border:1px solid #e4e7ee;border-radius:14px;padding:16px;margin:14px 0;background:#ffffff;">
                <div style="font-size:13px;color:#687082;font-weight:700;margin-bottom:6px;">
                    {html.escape(badge)} · skóre {html.escape(str(signal.get("score", 0)))} · {html.escape(signal.get("source", ""))} · {html.escape(age)}
                </div>
                <h2 style="font-size:18px;line-height:1.3;margin:0 0 8px 0;">
                    {html.escape(str(index))}. {html.escape(signal.get("title", ""))}
                </h2>
                <p style="margin:0 0 8px 0;color:#151922;"><strong>Firma:</strong> {html.escape(companies)}</p>
                <p style="margin:0 0 12px 0;color:#687082;"><strong>Důvody:</strong> {html.escape(reasons)}</p>
                <a href="{html.escape(signal.get("link", ""))}" style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;padding:9px 12px;border-radius:8px;font-weight:700;">
                    Otevřít zdroj
                </a>
            </div>
            """

    dashboard_button = ""
    if dashboard_url:
        dashboard_button = f"""
        <p style="margin:22px 0;">
            <a href="{html.escape(dashboard_url)}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:11px 14px;border-radius:9px;font-weight:700;">
                Otevřít celý dashboard
            </a>
        </p>
        """

    html_body = f"""<!doctype html>
<html lang="cs">
<head>
    <meta charset="utf-8">
    <title>Tender radar – {today}</title>
</head>
<body style="margin:0;background:#f5f6fa;font-family:Arial,Helvetica,sans-serif;color:#151922;">
    <div style="max-width:760px;margin:0 auto;padding:24px;">
        <div style="background:#111827;color:#ffffff;border-radius:18px;padding:24px;margin-bottom:18px;">
            <h1 style="margin:0 0 8px 0;font-size:28px;">Tender radar</h1>
            <p style="margin:0;color:#d1d5db;">TOP {EMAIL_TOP_LIMIT} signálů · {today}</p>
        </div>

        <div style="background:#ffffff;border:1px solid #e4e7ee;border-radius:18px;padding:18px;margin-bottom:18px;">
            <p style="margin:0 0 6px 0;"><strong>Silné signály:</strong> {total_strong}</p>
            <p style="margin:0;"><strong>Slabší signály k ověření:</strong> {total_fallback}</p>
            {dashboard_button}
        </div>

        {signal_items_html}

        <p style="font-size:12px;color:#687082;margin-top:22px;">
            Tento e-mail je automaticky generovaný přes GitHub Actions. Kompletní dashboard se dál ukládá do GitHub Pages.
        </p>
    </div>
</body>
</html>
"""

    return text_body, html_body


def send_email(subject, text_body, html_body):
    required_env = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "EMAIL_TO",
    ]

    missing = [name for name in required_env if not os.environ.get(name)]

    if missing:
        print(f"Email not sent. Missing environment variables: {', '.join(missing)}")
        return

    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ["SMTP_PORT"])
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]

    recipients = [
        email.strip()
        for email in os.environ["EMAIL_TO"].split(",")
        if email.strip()
    ]

    if not recipients:
        print("Email not sent. EMAIL_TO is empty.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, recipients, msg.as_string())

    print(f"Email sent to: {', '.join(recipients)}")


def main():
    config = load_config()
    strong_signals, fallback_signals, notes = collect_signals(config)

    full_text_body = build_text_report(strong_signals, fallback_signals, notes)
    full_html_body = build_html_report(strong_signals, fallback_signals, notes)

    output_path = save_html(full_html_body)

    email_text_body, email_html_body = build_email_bodies(
        strong_signals,
        fallback_signals,
    )

    subject = f"Tender radar – {datetime.now().strftime('%d.%m.%Y')}"

    send_email(subject, email_text_body, email_html_body)

    print(full_text_body)
    print("")
    print(f"HTML report saved to: {output_path}")


if __name__ == "__main__":
    main()
