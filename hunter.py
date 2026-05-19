#!/usr/bin/env python3
"""
FREELANCE-HUNTER
────────────────
Lancers  : RSS フィード（Cloudflare 回避）
CW       : Playwright ノーログイン（JS レンダリング対応）
Gemini   : 案件評価 + 提案文生成
Discord  : 通知
"""

import asyncio
import json
import os
import re
from pathlib import Path

import urllib.parse

import feedparser
import httpx
from bs4 import BeautifulSoup
import google.generativeai as genai

# ─── 環境変数（.env ファイルまたは環境変数から読み込み） ──────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_FREELANCE"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

# ─── 検索キーワード ────────────────────────────────────────────────────
KEYWORDS = [
    "Next.js", "React", "TypeScript", "Python",
    "FastAPI", "AIチャットボット", "LINE Bot",
    "Webアプリ", "LP制作", "ランディングページ",
    "管理画面", "ダッシュボード", "Supabase",
    "システム開発", "API開発", "フロントエンド",
    "Web制作", "Webサイト",
]

MIN_BUDGET = 15_000

MY_PROFILE = """
【スキル】
- Next.js / React / TypeScript（フロントエンド）
- Python / FastAPI（バックエンド API）
- AI チャットボット構築（Claude / Gemini / OpenAI API 統合）
- Supabase / PostgreSQL（スキーマ設計・RLS）
- 管理画面 / ダッシュボード / LP 制作
- LINE Bot / Discord Bot
- Web スクレイピング（Playwright）
- Vercel / Render（デプロイ・運用）

【実績】
- FX 分析 AI システム（FastAPI + Next.js + Supabase + Gemini）
- AI チャットボット SaaS（暗号化 URL 方式、DB 不要）
- リアルタイム KPI ダッシュボード（複数 API 統合）

経験: Web 開発 2 年、AI 統合案件多数
"""

# ─── 既通知済みURL管理 ────────────────────────────────────────────────
SEEN_FILE = Path("seen_jobs.json")

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


# ─── Gemini 評価 ───────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-2.0-flash")

def evaluate(title: str, description: str, budget: int) -> dict:
    prompt = f"""あなたは優秀なフリーランスエンジニアのマネージャーです。
以下の案件を評価し、JSONのみ返してください（前置き不要）。

【担当エンジニアのスキル】
{MY_PROFILE}

【案件情報】
タイトル: {title}
予算: ¥{budget:,}
説明: {description[:1000]}

返すJSON:
{{
  "score": 1〜10,
  "should_apply": true/false,
  "reason": "判断理由1文",
  "estimate_hours": 予想作業時間（整数）,
  "proposal": "提案文400字（です・ます調、そのまま送れる完成形）"
}}"""

    try:
        resp = _model.generate_content(prompt)
        m = re.search(r"\{.*\}", resp.text.strip(), re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [Gemini] {e}")
    return {"score": 0, "should_apply": False, "reason": "評価失敗", "proposal": "", "estimate_hours": 0}


# ─── Discord 通知 ──────────────────────────────────────────────────────
async def notify(job: dict, ev: dict, client: httpx.AsyncClient):
    score  = ev.get("score", 0)
    hours  = ev.get("estimate_hours", 0)
    hourly = job["budget"] // hours if hours > 0 else 0
    emoji  = "🟢" if score >= 7 else "🟡"
    label  = "【即応募】" if score >= 8 else "【応募推奨】"

    msg = (
        f"{emoji} {label} **{job['platform']}**\n\n"
        f"📋 **{job['title']}**\n"
        f"💰 予算: ¥{job['budget']:,}"
        + (f"　⏱ 推定{hours}h → 時給¥{hourly:,}" if hours > 0 else "")
        + f"\n⭐ 適合度: {score}/10 — {ev.get('reason','')}\n"
        f"🔗 {job['url']}\n\n"
        f"📝 **提案文（コピペ用）**\n```\n{ev.get('proposal','')}\n```"
    )
    await client.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    print(f"  ✓ [{score}/10] {job['title'][:40]}")


# ─── ユーティリティ ────────────────────────────────────────────────────
def parse_budget(text: str) -> int:
    nums = re.findall(r"[\d,]+", text.replace("，", ","))
    candidates = [int(n.replace(",", "")) for n in nums if 1_000 <= int(n.replace(",", "")) <= 10_000_000]
    return max(candidates) if candidates else 0

def dedupe(jobs: list) -> list:
    seen, out = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


# ─── DuckDuckGo 経由でジョブURLを収集 ────────────────────────────────
# GitHub Actions の IP は各プラットフォームで直接ブロックされる。
# DuckDuckGo は DDG 自身がインデックス済みのページを返すため迂回可能。
async def search_ddg(client: httpx.AsyncClient, query: str) -> list[str]:
    """DuckDuckGo HTML 検索 (POST) からURLリストを返す"""
    urls = []
    try:
        r = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        if r.status_code not in (200, 202):
            return []
        soup = BeautifulSoup(r.text, "lxml")
        for a in soup.select(".result__title a"):
            href = a.get("href", "")
            if href.startswith("http") and "duckduckgo.com" not in href:
                urls.append(href)
    except Exception as e:
        print(f"  [DDG error] {e}")
    return urls


async def scrape_lancers(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen_urls: set[str] = set()

    for kw in KEYWORDS[:10]:  # DDG レート制限のため上位10キーワードに絞る
        query = f"site:lancers.jp/work/detail {kw}"
        urls  = await search_ddg(client, query)
        await asyncio.sleep(0.5)  # DDG レート制限回避

        for url in urls:
            if "lancers.jp/work/detail" not in url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # 案件ページを直接取得（Lancers の個別ページは通常アクセス可）
            try:
                r = await client.get(url, timeout=20)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)

                # タイトル
                title_tag = soup.find("h1") or soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else url.split("/")[-1]

                budget = parse_budget(text)
                if budget < MIN_BUDGET:
                    continue

                jobs.append({
                    "platform": "ランサーズ",
                    "title": title[:80],
                    "url": url,
                    "budget": budget,
                    "description": text[:1200],
                })
            except Exception:
                continue

    result = dedupe(jobs)
    print(f"  [Lancers] {len(result)}件")
    return result


async def scrape_crowdworks(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    seen_urls: set[str] = set()

    for kw in KEYWORDS[:10]:
        query = f"crowdworks.jp/public/jobs {kw} 固定報酬 開発"
        urls  = await search_ddg(client, query)
        await asyncio.sleep(0.5)

        for url in urls:
            if "crowdworks.jp/public/jobs" not in url:
                continue
            if any(x in url for x in ["/search", "/category", "/skill"]):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                r = await client.get(url, timeout=20)
                if r.status_code != 200:
                    continue
                soup = BeautifulSoup(r.text, "lxml")
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)

                title_tag = soup.find("h1") or soup.find("title")
                title = title_tag.get_text(strip=True) if title_tag else "案件"

                budget = parse_budget(text)
                if budget < MIN_BUDGET:
                    continue

                jobs.append({
                    "platform": "クラウドワークス",
                    "title": title[:80],
                    "url": url,
                    "budget": budget,
                    "description": text[:1200],
                })
            except Exception:
                continue

    result = dedupe(jobs)
    print(f"  [CW] {len(result)}件")
    return result


# ─── メイン ───────────────────────────────────────────────────────────
async def main():
    print("=== FREELANCE-HUNTER 起動 ===")
    seen = load_seen()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        # DuckDuckGo 経由で Lancers / CW を並行スキャン
        lancers_jobs, cw_jobs = await asyncio.gather(
            scrape_lancers(client),
            scrape_crowdworks(client),
        )

        all_jobs = dedupe(lancers_jobs + cw_jobs)
        new_jobs  = [j for j in all_jobs if j["url"] not in seen]

        print(f"合計: {len(all_jobs)}件 / 新着: {len(new_jobs)}件")

        notified = 0
        for job in new_jobs:
            ev = evaluate(job["title"], job["description"], job["budget"])
            seen.add(job["url"])
            if ev.get("score", 0) >= 6:
                await notify(job, ev, client)
                notified += 1
                await asyncio.sleep(1)

        save_seen(seen)
        print(f"通知: {notified}件 / 完了")


if __name__ == "__main__":
    asyncio.run(main())
