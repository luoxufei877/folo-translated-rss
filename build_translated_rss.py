from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import feedparser
import requests


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources.json"
DEFAULT_OUTPUT = ROOT / "public"
DEFAULT_CACHE = ROOT / ".cache" / "translations.json"
USER_AGENT = "PersonalFoloTranslatedRSS/1.0"
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM_NS)

CATEGORY_SLUGS = {
    "经济": "economy",
    "科技": "technology",
    "国际": "international",
    "国内": "china",
}


@dataclass
class Article:
    key: str
    category: str
    source: str
    trust: str
    title: str
    summary: str
    link: str
    published: datetime


def clean_text(value: str, limit: int = 900) -> str:
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value or "", flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def source_url(source: dict[str, Any]) -> str:
    kind = source["type"]
    if kind == "rss":
        return source["url"]
    query = urllib.parse.quote_plus(source["query"])
    if kind == "google_news_cn":
        return f"https://news.google.com/rss/search?q={query}&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
    if kind == "google_news_global":
        return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    raise ValueError(f"Unsupported source type: {kind}")


def entry_time(entry: dict[str, Any], now: datetime) -> datetime:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    if not stamp:
        return now
    return datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc)


def normalized_title(value: str) -> str:
    value = re.sub(r"\s+-\s+[^-]{2,80}$", "", value)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.casefold())


def valid_news_title(value: str) -> bool:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", value))
    latin_words = re.findall(r"[A-Za-z]{2,}", value)
    if chinese >= 4:
        return True
    return len(value) >= 20 and len(latin_words) >= 4


def fetch_source(
    source: dict[str, Any], now: datetime, cutoff: datetime, max_per_source: int
) -> tuple[list[Article], str | None]:
    url = source_url(source)
    last_error: Exception | None = None
    response: requests.Response | None = None
    for _ in range(2):
        try:
            response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
            response.raise_for_status()
            break
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    if response is None or not response.ok:
        return [], f"{source['name']}: {type(last_error).__name__}: {last_error}"

    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        return [], f"{source['name']}: {parsed.bozo_exception}"

    items: list[Article] = []
    for entry in parsed.entries:
        title = clean_text(entry.get("title", ""), 350)
        link = str(entry.get("link", "")).strip()
        published = entry_time(entry, now)
        if not title or not valid_news_title(title) or not link or published < cutoff:
            continue
        publisher = ""
        if isinstance(entry.get("source"), dict):
            publisher = clean_text(entry["source"].get("title", ""), 120)
        summary = clean_text(entry.get("summary") or entry.get("description") or "")
        source_name = publisher or source["name"]
        identity = hashlib.sha256(
            f"{normalized_title(title)}|{source_name}|{published.date().isoformat()}".encode("utf-8")
        ).hexdigest()[:24]
        items.append(
            Article(
                key=identity,
                category=source["category"],
                source=source_name,
                trust=source.get("trust", "B"),
                title=title,
                summary=summary,
                link=link,
                published=published,
            )
        )
    items.sort(key=lambda row: row.published, reverse=True)
    return items[:max_per_source], None


def collect_articles(
    manifest: dict[str, Any], lookback_days: int, max_per_source: int
) -> tuple[dict[str, list[Article]], list[str]]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    grouped = {category: [] for category in CATEGORY_SLUGS}
    errors: list[str] = []
    for index, source in enumerate(manifest["sources"], 1):
        if source.get("category") not in grouped:
            continue
        items, error = fetch_source(source, now, cutoff, max_per_source)
        if error:
            errors.append(error)
            print(f"[{index:02d}] FAIL {source['name']}: {error}")
            continue
        grouped[source["category"]].extend(items)
        print(f"[{index:02d}] OK   {source['name']}: {len(items)}")

    for category, rows in grouped.items():
        deduped: list[Article] = []
        seen_links: set[str] = set()
        seen_titles: set[str] = set()
        for row in sorted(rows, key=lambda item: item.published, reverse=True):
            title_key = normalized_title(row.title)
            if row.link in seen_links or not title_key or title_key in seen_titles:
                continue
            seen_links.add(row.link)
            seen_titles.add(title_key)
            deduped.append(row)
        grouped[category] = deduped
    return grouped, errors


def has_meaningful_chinese(value: str) -> bool:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    return chinese >= 6 and chinese >= latin / 3


def load_cache(path: Path) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") == 1 and isinstance(payload.get("items"), dict):
            return payload["items"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_cache(path: Path, items: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_at": datetime.now(timezone.utc).isoformat(), "items": items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def translation_key(article: Article) -> str:
    material = f"{article.title}|{article.summary}|{article.source}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def parse_json_array(value: str) -> list[dict[str, Any]]:
    value = value.strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I | re.S)
    start, end = value.find("["), value.rfind("]")
    if start < 0 or end < start:
        raise ValueError("AI response does not contain a JSON array")
    parsed = json.loads(value[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("AI response is not a JSON array")
    return parsed


def translate_batch(
    rows: list[Article], api_key: str, api_base: str, model: str
) -> dict[str, dict[str, str]]:
    payload = [
        {
            "id": translation_key(row),
            "source": row.source,
            "title": row.title,
            "summary": row.summary[:700],
        }
        for row in rows
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "你是严谨的中文新闻翻译编辑。只能依据提供的标题和RSS摘要，不得补充材料中没有的事实。"
                "准确保留人名、机构、型号、数字和不确定性。"
            ),
        },
        {
            "role": "user",
            "content": (
                "把每条新闻处理为自然、准确的简体中文。title_zh为中文标题；summary_zh为40至100个中文字符的简介。"
                "若摘要信息不足，就简要翻译已有信息，不要推测。只输出严格JSON数组："
                '[{"id":"原id","title_zh":"...","summary_zh":"..."}]\n\n'
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
    ]
    endpoint = api_base.rstrip("/") + "/chat/completions"
    body = {
        "model": model.removeprefix("openai/"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 4000,
    }
    response: requests.Response | None = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=120,
            )
            response.raise_for_status()
            break
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
    if response is None or not response.ok:
        raise last_error or RuntimeError("translation request failed")
    content = response.json()["choices"][0]["message"]["content"]
    parsed = parse_json_array(content)
    result: dict[str, dict[str, str]] = {}
    expected = {translation_key(row) for row in rows}
    for item in parsed:
        item_id = str(item.get("id", ""))
        title_zh = clean_text(str(item.get("title_zh", "")), 350)
        summary_zh = clean_text(str(item.get("summary_zh", "")), 500)
        if item_id in expected and title_zh:
            result[item_id] = {"title_zh": title_zh, "summary_zh": summary_zh}
    return result


def translate_articles(
    grouped: dict[str, list[Article]], cache_path: Path, batch_size: int
) -> tuple[dict[str, dict[str, str]], list[str]]:
    cache = load_cache(cache_path)
    api_key = os.environ.get("AI_API_KEY", "").strip()
    api_base = os.environ.get("AI_API_BASE", "https://jizhiapi.site/v1").strip()
    model = os.environ.get("AI_MODEL", "gpt-5.4-mini").strip()
    pending: list[Article] = []
    for rows in grouped.values():
        for row in rows:
            key = translation_key(row)
            cached = cache.get(key)
            if cached and (has_meaningful_chinese(row.title) or has_meaningful_chinese(cached.get("title_zh", ""))):
                continue
            if cached:
                cache.pop(key, None)
            if has_meaningful_chinese(row.title) and (not row.summary or has_meaningful_chinese(row.summary)):
                cache[key] = {
                    "title_zh": row.title,
                    "summary_zh": row.summary or f"来源：{row.source}",
                }
            else:
                pending.append(row)

    errors: list[str] = []
    if pending and not api_key:
        raise RuntimeError("AI_API_KEY is required because untranslated articles were found")
    for offset in range(0, len(pending), batch_size):
        batch = pending[offset : offset + batch_size]
        try:
            translated = translate_batch(batch, api_key, api_base, model)
            cache.update(translated)
            print(f"TRANSLATE batch {offset // batch_size + 1}: {len(translated)}/{len(batch)}")
        except Exception as exc:
            message = f"translation batch {offset // batch_size + 1}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"WARN {message}")
        save_cache(cache_path, cache)
    save_cache(cache_path, cache)
    return cache, errors


def translated(article: Article, cache: dict[str, dict[str, str]]) -> tuple[str, str]:
    value = cache.get(translation_key(article), {})
    return value.get("title_zh", article.title), value.get("summary_zh", article.summary)


def build_feed(
    category: str,
    rows: list[Article],
    cache: dict[str, dict[str, str]],
    output: Path,
    public_base_url: str,
    max_items: int,
) -> None:
    slug = CATEGORY_SLUGS[category]
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = f"{category}｜可信来源中文聚合"
    ET.SubElement(channel, "link").text = public_base_url or "https://example.invalid/"
    ET.SubElement(channel, "description").text = "标题与简介已自动翻译为中文，点击标题查看原始来源。"
    ET.SubElement(channel, "language").text = "zh-CN"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        href=f"{public_base_url.rstrip('/')}/feeds/{slug}-v2.xml",
        rel="self",
        type="application/rss+xml",
    )
    for row in rows[:max_items]:
        title_zh, summary_zh = translated(row, cache)
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title_zh
        ET.SubElement(item, "link").text = row.link
        guid = ET.SubElement(item, "guid", isPermaLink="false")
        guid.text = row.key
        ET.SubElement(item, "pubDate").text = format_datetime(row.published)
        original = "" if title_zh == row.title else f"<p><small>原标题：{html.escape(row.title)}</small></p>"
        description = (
            f"<p>{html.escape(summary_zh)}</p>"
            f"<p><strong>来源：</strong>{html.escape(row.source)}｜可信度 {html.escape(row.trust)}</p>"
            f"{original}<p><a href=\"{html.escape(row.link, quote=True)}\">查看原文</a></p>"
        )
        ET.SubElement(item, "description").text = description
    ET.indent(rss, space="  ")
    target = output / "feeds" / f"{slug}-v2.xml"
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(rss).write(target, encoding="utf-8", xml_declaration=True)
    ET.ElementTree(rss).write(output / "feeds" / f"{slug}.xml", encoding="utf-8", xml_declaration=True)


def build_opml(output: Path, public_base_url: str) -> None:
    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = "可信新闻中文聚合｜经济·科技·国际·国内"
    body = ET.SubElement(opml, "body")
    for category, slug in CATEGORY_SLUGS.items():
        parent = ET.SubElement(body, "outline", text=category, title=category)
        ET.SubElement(
            parent,
            "outline",
            text=f"{category}｜自动中文翻译 V2",
            title=f"{category}｜自动中文翻译 V2",
            type="rss",
            xmlUrl=f"{public_base_url.rstrip('/')}/feeds/{slug}-v2.xml",
        )
    ET.indent(opml, space="  ")
    ET.ElementTree(opml).write(output / "folo-translated-v2.opml", encoding="utf-8", xml_declaration=True)
    ET.ElementTree(opml).write(output / "folo-translated.opml", encoding="utf-8", xml_declaration=True)


def build_index(output: Path, grouped: dict[str, list[Article]], public_base_url: str) -> None:
    links = "\n".join(
        f'<li><a href="feeds/{slug}-v2.xml">{category}中文RSS V2</a>（{len(grouped[category])}条）</li>'
        for category, slug in CATEGORY_SLUGS.items()
    )
    opml_link = '<p><a href="folo-translated-v2.opml">下载Folo导入文件 V2</a></p>' if public_base_url else ""
    page = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>可信新闻中文RSS</title><style>body{{max-width:760px;margin:48px auto;padding:0 20px;font:17px/1.7 system-ui}}a{{color:#e6502f}}</style>
<h1>可信新闻中文RSS</h1><p>免费流水线抓取可信来源，并将标题和简介翻译为中文；文章链接保持指向原始来源。</p>
<ul>{links}</ul>{opml_link}<p>更新时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}</p>
</html>"""
    output.mkdir(parents=True, exist_ok=True)
    (output / "index.html").write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--lookback-days", type=int, default=4)
    parser.add_argument("--max-per-source", type=int, default=12)
    parser.add_argument("--max-per-category", type=int, default=160)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--public-base-url", default=os.environ.get("PUBLIC_BASE_URL", ""))
    args = parser.parse_args()

    manifest = json.loads(args.sources.read_text(encoding="utf-8"))
    grouped, fetch_errors = collect_articles(manifest, args.lookback_days, args.max_per_source)
    cache, translation_errors = translate_articles(grouped, args.cache, args.batch_size)
    public_base_url = args.public_base_url.rstrip("/")
    for category, rows in grouped.items():
        build_feed(category, rows, cache, args.output, public_base_url, args.max_per_category)
    if public_base_url:
        build_opml(args.output, public_base_url)
    build_index(args.output, grouped, public_base_url)
    print("RESULT " + ", ".join(f"{category}={len(rows)}" for category, rows in grouped.items()))
    print(f"OUTPUT {args.output}")
    if fetch_errors or translation_errors:
        print(f"WARNINGS fetch={len(fetch_errors)} translation={len(translation_errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
