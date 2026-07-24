from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit, urlunsplit

import feedparser
import requests
import yaml

CONFIG_PATH = Path("config.yaml")
HISTORY_PATH = Path("history.json")
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "AI-Demand-Monitoring/1.0"
KST = timezone(timedelta(hours=9))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("ai-demand-monitoring")


@dataclass(frozen=True)
class Article:
    category: str
    topic: str
    title: str
    url: str
    source: str
    published: datetime


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a mapping.")
    return config


def load_history() -> list[str]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        urls = data.get("seen_urls", [])
        return urls if isinstance(urls, list) else []
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("Invalid history.json; starting with empty history.")
        return []


def save_history(keys: list[str], limit: int) -> None:
    deduplicated = list(dict.fromkeys(keys))[-limit:]
    temporary = HISTORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"seen_urls": deduplicated}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(HISTORY_PATH)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(
        item for item in parts.query.split("&")
        if item and not item.lower().startswith(("utm_", "gclid=", "fbclid="))
    )
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))


def url_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def entry_datetime(entry: Any) -> datetime:
    for name in ("published_parsed", "updated_parsed"):
        value = getattr(entry, name, None)
        if value:
            return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_articles(query: str, category: str, topic: str, cutoff: datetime) -> list[Article]:
    url = GOOGLE_NEWS_RSS.format(query=quote_plus(query))
    response = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    feed = feedparser.parse(response.content)

    result: list[Article] = []
    for entry in feed.entries:
        published = entry_datetime(entry)
        if published < cutoff:
            continue

        title = clean_text(getattr(entry, "title", ""))
        link = canonical_url(getattr(entry, "link", ""))
        if not title or not link:
            continue

        source_obj = getattr(entry, "source", None)
        source = clean_text(getattr(source_obj, "title", "")) if source_obj else "Unknown"
        result.append(Article(category, topic, title, link, source or "Unknown", published))
    return result


def collect(config: dict[str, Any], historical_keys: set[str]) -> list[Article]:
    project = config["project"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(project.get("lookback_hours", 96)))
    per_topic = int(project.get("max_articles_per_topic", 5))
    max_total = int(project.get("max_total_articles", 40))

    collected: list[Article] = []
    current_keys: set[str] = set()

    for category in config.get("categories", []):
        for topic in category.get("topics", []):
            candidates: list[Article] = []
            for query in topic.get("queries", []):
                try:
                    candidates.extend(fetch_articles(query, category["name"], topic["name"], cutoff))
                except requests.RequestException as error:
                    logger.warning("RSS request failed for %r: %s", query, error)

            candidates.sort(key=lambda article: article.published, reverse=True)
            topic_count = 0
            for article in candidates:
                key = url_key(article.url)
                if key in historical_keys or key in current_keys:
                    continue
                collected.append(article)
                current_keys.add(key)
                topic_count += 1
                if topic_count >= per_topic or len(collected) >= max_total:
                    break
            if len(collected) >= max_total:
                return collected
    return collected


def summarize(config: dict[str, Any], articles: list[Article]) -> str:
    if not articles:
        return "이번 실행에서 새롭게 확인된 기사가 없습니다."

    return (
        f"이번 모니터링에서 신규 기사 {len(articles)}건을 수집했습니다.\n\n"
        "아래에서 카테고리별 기사 제목과 출처를 확인할 수 있습니다."
    )


def build_bodies(config: dict[str, Any], summary: str, articles: list[Article]) -> tuple[str, str]:
    grouped: dict[str, dict[str, list[Article]]] = {}
    for article in articles:
        grouped.setdefault(article.category, {}).setdefault(article.topic, []).append(article)

    sections: list[str] = []
    for category in config.get("categories", []):
        topics: list[str] = []
        for topic in category.get("topics", []):
            items = grouped.get(category["name"], {}).get(topic["name"], [])
            if not items:
                continue
            rows = "".join(
                f'<li style="margin-bottom:12px"><a href="{html.escape(a.url)}">{html.escape(a.title)}</a><br>'
                f'<span style="color:#666;font-size:13px">{html.escape(a.source)} · '
                f'{a.published.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")}</span></li>'
                for a in items
            )
            topics.append(f"<h3>{html.escape(topic['name'])}</h3><ul>{rows}</ul>")
        if topics:
            sections.append(f"<h2>{html.escape(category['name'])}</h2>{''.join(topics)}")

    title = html.escape(config["project"].get("title", "AI Demand Monitoring"))
    summary_html = "<br>".join(html.escape(summary).splitlines())
    generated = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    html_body = (
        "<html><body style=\"font-family:Arial,'Apple SD Gothic Neo',sans-serif;line-height:1.6;"
        "max-width:820px;margin:auto;padding:24px\">"
        f"<h1>{title}</h1><p style=\"color:#666\">Generated: {generated}</p>"
        f"<div style=\"background:#f5f7fa;padding:18px;border-radius:10px\">"
        f"<h2>Executive Briefing</h2><p>{summary_html}</p></div>"
        f"{''.join(sections) if sections else '<p>새로운 기사가 없습니다.</p>'}</body></html>"
    )

    text_lines = [config["project"].get("title", "AI Demand Monitoring"), "", summary, ""]
    for article in articles:
        text_lines.append(f"- [{article.category} / {article.topic}] {article.title}\n  {article.url}")
    return html_body, "\n".join(text_lines)


def send_email(config: dict[str, Any], html_body: str, text_body: str) -> None:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing secrets: " + ", ".join(missing))

    sender = os.environ["EMAIL_FROM"]
    recipients = [x.strip() for x in os.environ["EMAIL_TO"].split(",") if x.strip()]
    message = MIMEMultipart("alternative")
    prefix = config.get("email", {}).get("subject_prefix", "[AI Demand Monitoring]")
    message["Subject"] = f"{prefix} {datetime.now(KST).strftime('%Y-%m-%d')}"
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText(text_body, "plain", "utf-8"))
    message.attach(MIMEText(html_body, "html", "utf-8"))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    context = ssl.create_default_context()

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.sendmail(sender, recipients, message.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.sendmail(sender, recipients, message.as_string())


def main() -> int:
    try:
        config = load_config()
        history = load_history()
        articles = collect(config, set(history))
        logger.info("New articles collected: %d", len(articles))
        summary = summarize(config, articles)
        html_body, text_body = build_bodies(config, summary, articles)
        send_email(config, html_body, text_body)
        save_history(
            history + [url_key(article.url) for article in articles],
            int(config["project"].get("history_limit", 3000)),
        )
        logger.info("Newsletter sent and history updated.")
        return 0
    except Exception as error:
        logger.exception("Execution failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
