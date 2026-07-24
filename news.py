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
from difflib import SequenceMatcher
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
USER_AGENT = "AI-Demand-Monitoring/2.0"
KST = timezone(timedelta(hours=9))
TEMPLATE_VERSION = "AIM-648-grouped-v6"

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
    description: str = ""


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a mapping.")
    return config


def load_history() -> dict[str, list[str]]:
    empty = {"seen_urls": [], "recent_titles": []}
    if not HISTORY_PATH.exists():
        return empty

    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        return {
            "seen_urls": data.get("seen_urls", []) if isinstance(data.get("seen_urls", []), list) else [],
            "recent_titles": data.get("recent_titles", []) if isinstance(data.get("recent_titles", []), list) else [],
        }
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("Invalid history.json; starting with empty history.")
        return empty


def save_history(
    seen_urls: list[str],
    recent_titles: list[str],
    url_limit: int,
    title_limit: int,
) -> None:
    payload = {
        "seen_urls": list(dict.fromkeys(seen_urls))[-url_limit:],
        "recent_titles": list(dict.fromkeys(recent_titles))[-title_limit:],
    }
    temporary = HISTORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(HISTORY_PATH)


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(
        item
        for item in parts.query.split("&")
        if item and not item.lower().startswith(("utm_", "gclid=", "fbclid="))
    )
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, query, ""))


def url_key(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def normalize_title(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"\[[^\]]+\]", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"[^0-9a-z가-힣]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def title_similarity(left: str, right: str) -> float:
    left_n = normalize_title(left)
    right_n = normalize_title(right)
    if not left_n or not right_n:
        return 0.0
    return SequenceMatcher(None, left_n, right_n).ratio()


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
        description = clean_text(
            getattr(entry, "summary", "") or getattr(entry, "description", "")
        )
        if not title or not link:
            continue

        source_obj = getattr(entry, "source", None)
        source = clean_text(getattr(source_obj, "title", "")) if source_obj else "Unknown"

        result.append(
            Article(
                category=category,
                topic=topic,
                title=title,
                url=link,
                source=source or "Unknown",
                published=published,
                description=description,
            )
        )
    return result


def collect(
    config: dict[str, Any],
    historical_keys: set[str],
    historical_titles: list[str],
) -> list[Article]:
    project = config["project"]
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=int(project.get("lookback_hours", 96))
    )
    per_topic = int(project.get("max_articles_per_topic", 5))
    max_total = int(project.get("max_total_articles", 40))

    threshold = float(project.get("dedup_similarity", 0.80))
    if threshold > 1:
        threshold /= 100
    threshold = max(0.0, min(threshold, 1.0))

    collected: list[Article] = []
    current_keys: set[str] = set()
    comparison_titles = list(historical_titles)

    for category in config.get("categories", []):
        for topic in category.get("topics", []):
            candidates: list[Article] = []

            for query in topic.get("queries", []):
                try:
                    candidates.extend(
                        fetch_articles(query, category["name"], topic["name"], cutoff)
                    )
                except requests.RequestException as error:
                    logger.warning("RSS request failed for %r: %s", query, error)

            candidates.sort(key=lambda article: article.published, reverse=True)
            topic_count = 0

            for article in candidates:
                key = url_key(article.url)

                if key in historical_keys or key in current_keys:
                    continue

                if any(
                    title_similarity(article.title, previous) >= threshold
                    for previous in comparison_titles
                ):
                    logger.info("Similar title excluded: %s", article.title)
                    continue

                collected.append(article)
                current_keys.add(key)
                comparison_titles.append(article.title)
                topic_count += 1

                if topic_count >= per_topic or len(collected) >= max_total:
                    break

            if len(collected) >= max_total:
                return collected

    return collected


def summarize(config: dict[str, Any], articles: list[Article]) -> str:
    if not articles:
        return "이번 실행에서 새롭게 확인된 기사가 없습니다."
    return f"이번 모니터링에서 신규 기사 {len(articles)}건을 수집했습니다."


def source_domain(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def prettify_source(value: str) -> str:
    value = clean_text(value)
    if not value or value.lower() == "unknown":
        return "Unknown"

    if "." in value and " " not in value:
        name = value.lower().removeprefix("www.").split(".")[0]
        return name.replace("-", " ").title()

    return value


def display_source(config: dict[str, Any], article: Article) -> str:
    mappings = config.get("source_names", {})
    if not isinstance(mappings, dict):
        mappings = {}

    normalized = {
        str(key).strip().lower().removeprefix("www."): str(value).strip()
        for key, value in mappings.items()
    }

    domain = source_domain(article.url)
    rss_source = article.source.lower().removeprefix("www.")

    for key in (domain, rss_source):
        if key in normalized:
            return normalized[key]

    if article.source and article.source.lower() != "unknown":
        return prettify_source(article.source)

    return prettify_source(domain)


def build_bodies(
    config: dict[str, Any],
    summary: str,
    articles: list[Article],
) -> tuple[str, str]:
    project_title = str(config.get("project", {}).get("title", "AI Demand Monitoring"))

    masthead = config.get("masthead", {})
    masthead_enabled = bool(masthead.get("enabled", True))

    # Keep the newsletter masthead fixed to the requested name.
    masthead_title = "AI Demand Monitoring"
    generated = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    # Fixed classification structure and display order.
    category_structure = [
        (
            "1. 수요",
            [
                ("1-1. AI 토큰 사용량", "AI 토큰 사용량"),
                ("1-2. AI 모델 업체 실적 전망", "AI 모델 업체 실적 전망"),
                ("1-3. 신규 AI 모델 출시", "신규 AI 모델 출시"),
            ],
        ),
        (
            "2. 플랫폼",
            [
                ("2-1. Hyperscaler CapEx / FCF", "Hyperscaler CapEx / FCF"),
                ("2-2. Hyperscaler 신용등급", "Hyperscaler 신용등급"),
            ],
        ),
        (
            "3. 인프라 및 환경",
            [
                ("3-1. 전력 수급", "전력 수급"),
                ("3-2. 메모리(HBM/DRAM/NAND) 수급", "메모리(HBM/DRAM/NAND) 수급"),
            ],
        ),
    ]

    # Group collected articles by their configured topic name.
    grouped: dict[str, list[Article]] = {}
    for article in articles:
        grouped.setdefault(article.topic.strip(), []).append(article)

    for topic_articles in grouped.values():
        topic_articles.sort(key=lambda item: item.published, reverse=True)

    toc_sections: list[str] = []
    body_sections: list[str] = []
    text_lines: list[str] = [
        masthead_title if masthead_enabled else project_title,
        project_title,
        generated,
        "",
        summary,
        "",
        "뉴스 목차",
    ]

    article_index = 1

    for category_title, topics in category_structure:
        toc_topic_rows: list[str] = []
        body_topic_sections: list[str] = []

        text_lines.extend(["", category_title])

        for topic_display, topic_key in topics:
            topic_articles = grouped.get(topic_key, [])

            toc_article_rows: list[str] = []
            body_article_blocks: list[str] = []

            text_lines.append(topic_display)

            for article in topic_articles:
                anchor = f"article-{article_index:03d}"
                source = display_source(config, article)
                heading = f"[{source}]{article.title}"
                published = article.published.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
                description = article.description or "RSS에 별도 기사 요약이 제공되지 않았습니다."

                toc_article_rows.append(
                    '<tr>'
                    '<td style="width:38px;padding:4px 0 4px 18px;vertical-align:top;'
                    'color:#6b7280;font-size:10pt;">'
                    f"{article_index:02d}"
                    "</td>"
                    '<td style="padding:4px 0;vertical-align:top;">'
                    f'<a href="#{anchor}" style="color:#1155cc;text-decoration:none;">'
                    f"{html.escape(heading)}</a>"
                    "</td>"
                    "</tr>"
                )

                body_article_blocks.append(
                    f'<div id="{anchor}" style="margin:0 0 14px 0;padding:0;">'
                    f'<div style="margin:0;font-weight:700;">'
                    f'<a href="{html.escape(article.url, quote=True)}" '
                    'style="color:#1155cc;text-decoration:none;">'
                    f"{html.escape(heading)}</a>"
                    "</div>"
                    f'<div style="margin:0;color:#6b7280;font-size:10pt;">'
                    f"{published}"
                    "</div>"
                    f'<div style="margin:0;">{html.escape(description)}</div>'
                    "</div>"
                )

                text_lines.extend(
                    [
                        heading,
                        published,
                        description,
                        article.url,
                        "",
                    ]
                )
                article_index += 1

            if toc_article_rows:
                toc_articles_html = (
                    '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
                    'border="0" style="width:100%;border-collapse:collapse;">'
                    f"{''.join(toc_article_rows)}"
                    "</table>"
                )
            else:
                toc_articles_html = (
                    '<div style="padding:3px 0 5px 18px;color:#9ca3af;font-size:10pt;">'
                    "해당 기간 신규 기사 없음"
                    "</div>"
                )

            toc_topic_rows.append(
                '<div style="margin:7px 0 0 0;">'
                f'<div style="font-weight:700;">{html.escape(topic_display)}</div>'
                f"{toc_articles_html}"
                "</div>"
            )

            if body_article_blocks:
                body_articles_html = "".join(body_article_blocks)
            else:
                body_articles_html = (
                    '<div style="margin:0;color:#9ca3af;">해당 기간 신규 기사 없음</div>'
                )

            body_topic_sections.append(
                '<div style="margin:0 0 20px 0;">'
                f'<div style="margin:0 0 8px 0;padding:7px 10px;background:#eef3f8;'
                'border-left:4px solid #5b9bd5;font-weight:700;">'
                f"{html.escape(topic_display)}"
                "</div>"
                f"{body_articles_html}"
                "</div>"
            )

        toc_sections.append(
            '<div style="margin:0 0 15px 0;">'
            f'<div style="margin:0 0 5px 0;padding:7px 10px;background:#d9e6f2;'
            'font-weight:700;font-size:12pt;">'
            f"{html.escape(category_title)}"
            "</div>"
            f"{''.join(toc_topic_rows)}"
            "</div>"
        )

        body_sections.append(
            '<div style="margin:0 0 26px 0;">'
            f'<div style="margin:0 0 12px 0;padding:9px 12px;background:#17365d;'
            'color:#ffffff;font-weight:700;font-size:13pt;">'
            f"{html.escape(category_title)}"
            "</div>"
            f"{''.join(body_topic_sections)}"
            "</div>"
        )

    if masthead_enabled:
        masthead_html = (
            '<table role="presentation" width="648" cellspacing="0" cellpadding="0" border="0" '
            'style="width:648px;max-width:648px;border-collapse:collapse;">'
            '<tr><td style="background:#17365d;color:#ffffff;padding:22px 24px 18px 24px;'
            'text-align:center;font-family:\'Malgun Gothic\',\'맑은 고딕\',Arial,sans-serif;'
            'font-size:22pt;font-weight:700;line-height:1.2;">'
            f"{html.escape(masthead_title)}"
            '</td></tr>'
            '<tr><td style="height:5px;background:#5b9bd5;font-size:0;line-height:0;">&nbsp;</td></tr>'
            "</table>"
        )
    else:
        masthead_html = ""

    toc_html = (
        '<div style="margin:18px 0 22px 0;padding:14px 18px;background:#f3f4f6;'
        'border:1px solid #d1d5db;">'
        '<div style="margin:0 0 10px 0;font-weight:700;font-size:13pt;">뉴스 목차</div>'
        f"{''.join(toc_sections)}"
        "</div>"
    )

    html_body = (
        '<!doctype html><html><head><meta charset="utf-8"></head>'
        '<body style="margin:0;padding:0;background:#ffffff;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        'style="width:100%;border-collapse:collapse;background:#ffffff;">'
        '<tr><td align="center">'
        '<table role="presentation" width="648" cellspacing="0" cellpadding="0" border="0" '
        'style="width:648px;max-width:648px;border-collapse:collapse;">'
        '<tr><td style="width:648px;max-width:648px;padding:0;'
        'font-family:\'Malgun Gothic\',\'맑은 고딕\',Arial,sans-serif;'
        'font-size:11pt;line-height:1.45;color:#111111;'
        'word-break:break-word;overflow-wrap:anywhere;">'
        f"{masthead_html}"
        '<div style="width:648px;max-width:648px;box-sizing:border-box;padding:18px 0 8px 0;">'
        f'<div style="margin:0;font-weight:700;font-size:14pt;">{html.escape(project_title)}</div>'
        f'<div style="margin:2px 0 0 0;color:#6b7280;font-size:10pt;">{generated}</div>'
        f'<div style="margin:14px 0 0 0;padding:12px 14px;background:#fff8dc;'
        'border-left:4px solid #d6a700;">'
        f"{html.escape(summary)}"
        "</div>"
        f"{toc_html}"
        '<div style="margin:0 0 12px 0;font-weight:700;font-size:13pt;">기사 본문</div>'
        f"{''.join(body_sections)}"
        '<div style="margin-top:18px;padding-top:10px;border-top:1px solid #d1d5db;'
        'color:#9ca3af;font-size:9pt;text-align:right;">'
        f"Template: {TEMPLATE_VERSION}"
        "</div>"
        "</div>"
        "</td></tr></table>"
        "</td></tr></table>"
        "</body></html>"
    )

    text_lines.append(f"Template: {TEMPLATE_VERSION}")
    return html_body, "\n".join(text_lines).rstrip()

def send_email(config: dict[str, Any], html_body: str, text_body: str) -> None:
    required = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "EMAIL_FROM",
        "EMAIL_TO",
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing secrets: " + ", ".join(missing))

    sender = os.environ["EMAIL_FROM"]
    recipients = [value.strip() for value in os.environ["EMAIL_TO"].split(",") if value.strip()]

    message = MIMEMultipart("alternative")
    prefix = config.get("email", {}).get("subject_prefix", "[AI Demand Monitoring]")
    message["Subject"] = f"{prefix} {datetime.now(KST).strftime('%Y-%m-%d')} | {TEMPLATE_VERSION}"
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

        articles = collect(
            config=config,
            historical_keys=set(history["seen_urls"]),
            historical_titles=history["recent_titles"],
        )
        logger.info("Template loaded: %s", TEMPLATE_VERSION)
        logger.info("New articles collected: %d", len(articles))

        summary = summarize(config, articles)
        html_body, text_body = build_bodies(config, summary, articles)
        send_email(config, html_body, text_body)

        project = config.get("project", {})
        save_history(
            seen_urls=history["seen_urls"] + [url_key(article.url) for article in articles],
            recent_titles=history["recent_titles"] + [article.title for article in articles],
            url_limit=int(project.get("history_limit", 3000)),
            title_limit=int(project.get("title_history_limit", 1000)),
        )

        logger.info("Newsletter sent and history updated.")
        return 0
    except Exception as error:
        logger.exception("Execution failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
