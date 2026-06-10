import html
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import yaml
from dateutil import parser as date_parser


@dataclass
class Hit:
    company: str
    title: str
    source: str
    url: str
    published: Optional[datetime]
    score: int
    level: str
    matched_terms: List[str]
    recommendation: str
    summary: str


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def parse_date(entry: Any) -> Optional[datetime]:
    for key in ("published", "updated", "created"):
        raw = getattr(entry, key, None) or entry.get(key)
        if raw:
            try:
                dt = date_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    # feedparser sometimes exposes parsed structs
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None) or entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def load_config(path: str = "config.yaml") -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def text_contains_company(text: str, company: str) -> bool:
    # Word-ish boundary. Allows names like T-Mobile and O2.
    return re.search(rf"(?<!\w){re.escape(company)}(?!\w)", text, flags=re.IGNORECASE) is not None


def find_signal(text: str, config: Dict[str, Any]) -> Tuple[int, str, List[str]]:
    best_score = 0
    best_level = "none"
    matched: List[str] = []
    for level, data in config["signals"].items():
        level_score = int(data["score"])
        terms = data.get("terms", [])
        level_matches = [term for term in terms if re.search(re.escape(term), text, flags=re.IGNORECASE)]
        if level_matches and level_score > best_score:
            best_score = level_score
            best_level = level
            matched = level_matches
    return best_score, best_level, matched


def classify_hits(config: Dict[str, Any]) -> Tuple[List[Hit], List[str]]:
    now = datetime.now(timezone.utc)
    lookback = timedelta(hours=int(config.get("lookback_hours", 30)))
    min_score = int(config.get("min_score_to_report", 25))
    hits: List[Hit] = []
    errors: List[str] = []
    seen_urls = set()

    for feed in config.get("feeds", []):
        source = feed.get("name", "Unknown source")
        url = feed.get("url")
        if not url:
            continue
        parsed = feedparser.parse(url)
        if parsed.bozo:
            errors.append(f"{source}: feed warning/error: {getattr(parsed, 'bozo_exception', 'unknown error')}")
        for entry in parsed.entries:
            title = strip_html(entry.get("title", ""))
            summary = strip_html(entry.get("summary", ""))
            link = entry.get("link", "")
            if not title or not link or link in seen_urls:
                continue
            seen_urls.add(link)
            published = parse_date(entry)
            if published and now - published > lookback:
                continue
            text = f"{title} {summary}"
            score, level, terms = find_signal(text, config)
            if score < min_score:
                continue
            matched_companies = [c for c in config.get("companies", []) if text_contains_company(text, c)]
            if not matched_companies:
                # Keep explicit tender/agency market news even if no watchlist company is present.
                matched_companies = ["Bez konkrétní firmy"] if score >= 80 else []
            for company in matched_companies:
                hits.append(Hit(
                    company=company,
                    title=title,
                    source=source,
                    url=link,
                    published=published,
                    score=score,
                    level=level,
                    matched_terms=terms,
                    recommendation=config.get("recommendations", {}).get(level, "Prověřit ručně."),
                    summary=summary[:400],
                ))
    hits.sort(key=lambda h: (h.score, h.published or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return hits[: int(config.get("max_items_in_email", 25))], errors


def fmt_date(dt: Optional[datetime]) -> str:
    if not dt:
        return "neuvedeno"
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def build_email(config: Dict[str, Any], hits: List[Hit], errors: List[str]) -> Tuple[str, str, str]:
    today = datetime.now().strftime("%d.%m.%Y")
    subject = f"{config.get('email', {}).get('subject_prefix', 'Tender radar')} – {today} – {len(hits)} signálů"

    if hits:
        text_lines = [f"Tender radar – {today}", "", f"Nalezeno signálů: {len(hits)}", ""]
        html_rows = []
        for i, h in enumerate(hits, 1):
            terms = ", ".join(h.matched_terms[:5])
            text_lines.extend([
                f"{i}. {h.company} – skóre {h.score}",
                f"Signál: {h.title}",
                f"Zdroj: {h.source} | {fmt_date(h.published)}",
                f"Shoda: {terms}",
                f"Doporučení: {h.recommendation}",
                f"Odkaz: {h.url}",
                "",
            ])
            html_rows.append(f"""
            <tr>
              <td style='padding:10px;border-bottom:1px solid #ddd;'>{i}</td>
              <td style='padding:10px;border-bottom:1px solid #ddd;'><strong>{html.escape(h.company)}</strong></td>
              <td style='padding:10px;border-bottom:1px solid #ddd;'>{h.score}</td>
              <td style='padding:10px;border-bottom:1px solid #ddd;'><a href='{html.escape(h.url)}'>{html.escape(h.title)}</a><br><small>{html.escape(h.source)} | {fmt_date(h.published)}</small></td>
              <td style='padding:10px;border-bottom:1px solid #ddd;'>{html.escape(terms)}</td>
              <td style='padding:10px;border-bottom:1px solid #ddd;'>{html.escape(h.recommendation)}</td>
            </tr>
            """)
        html_body = f"""
        <html><body style='font-family:Arial,sans-serif;'>
        <h2>Tender radar – {today}</h2>
        <p>Nalezeno signálů: <strong>{len(hits)}</strong></p>
        <table style='border-collapse:collapse;width:100%;font-size:14px;'>
          <thead>
            <tr style='background:#f2f2f2;'>
              <th style='text-align:left;padding:10px;'>#</th>
              <th style='text-align:left;padding:10px;'>Firma</th>
              <th style='text-align:left;padding:10px;'>Skóre</th>
              <th style='text-align:left;padding:10px;'>Signál</th>
              <th style='text-align:left;padding:10px;'>Shoda</th>
              <th style='text-align:left;padding:10px;'>Doporučení</th>
            </tr>
          </thead>
          <tbody>{''.join(html_rows)}</tbody>
        </table>
        """
    else:
        text_lines = [f"Tender radar – {today}", "", "Dnes nebyly nalezeny žádné signály nad nastaveným prahem."]
        html_body = f"<html><body style='font-family:Arial,sans-serif;'><h2>Tender radar – {today}</h2><p>Dnes nebyly nalezeny žádné signály nad nastaveným prahem.</p>"

    if errors:
        text_lines.extend(["", "Poznámky ke zdrojům:", *[f"- {e}" for e in errors[:10]]])
        html_body += "<h3>Poznámky ke zdrojům</h3><ul>" + "".join(f"<li>{html.escape(e)}</li>" for e in errors[:10]) + "</ul>"
    html_body += "</body></html>"
    return subject, "\n".join(text_lines), html_body


def send_email(config: Dict[str, Any], subject: str, text_body: str, html_body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    mail_to = os.environ["MAIL_TO"]
    mail_from = os.environ.get("MAIL_FROM", smtp_user)
    from_name = config.get("email", {}).get("from_name", "Tender radar")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{mail_from}>"
    msg["To"] = mail_to
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(smtp_user, smtp_password)
        server.sendmail(mail_from, [x.strip() for x in mail_to.split(",")], msg.as_string())


def main() -> None:
    config = load_config()
    hits, errors = classify_hits(config)
    subject, text_body, html_body = build_email(config, hits, errors)
    print(text_body)
    if os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes"):
        print("\nDRY_RUN=true, e-mail not sent.")
        return
    send_email(config, subject, text_body, html_body)


if __name__ == "__main__":
    main()
