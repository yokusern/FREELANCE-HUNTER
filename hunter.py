#!/usr/bin/env python3
"""
FREELANCE-HUNTER 最強版
────────────────────────
httpx + BeautifulSoup で高速スキャン（ブラウザ不要）
Gemini で案件評価 + 提案文生成 → Discord 通知
毎時間自動起動
"""

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
import google.generativeai as genai

# ─── 環境変数 ──────────────────────────────────────────────────────────
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_FREELANCE"]
GEMINI_API_KEY  = os.environ["GEMINI_API_KEY"]

# ─── リクエストヘッダー（実ブラウザに見せかける） ────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# ─── 検索キーワード（幅広くとって Gemini で絞る） ──────────────────────
KEYWORDS = [
    "Next.js", "React", "TypeScript", "Python",
    "FastAPI", "AIチャットボット", "LINE Bot",
    "Webアプリ", "LP制作", "ランディングページ",
    "管理画面", "ダッシュボード", "Supabase",
    "システム開発", "API開発", "フロントエンド",
    "Web制作", "Webサイト作成",
]

# ─── フィルタ ──────────────────────────────────────────────────────────
MIN_BUDGET = 15_000   # 1.5万円以上

# ─── 自分のプロフィール ────────────────────────────────────────────────
MY_PROFILE = """
【スキル】
- Next.js / React / TypeScript（フロントエンド）
- Python / FastAPI（バックエンド API 設計・実装）
- AI チャットボット構築（Claude / Gemini / OpenAI API 統合）
- Supabase / PostgreSQL（スキーマ設計・RLS・認証）
- 管理画面 / ダッシュボード / LP 制作
- LINE Bot / Discord Bot
- Web スクレイピング（Playwright / BeautifulSoup）
- Vercel / Render（デプロイ・運用）

【実績】
- FX 分析 AI システム（FastAPI + Next.js + Supabase + Gemini 統合）
- 暗号化 URL 方式の AI チャットボット SaaS（DB 不要、B2B 向け）
- リアルタイム KPI ダッシュボード（複数 API 統合、自動更新）

経験: Web 開発 2 年、AI 統合案件多数、全案件 Vercel/Render でデプロイ実績あり
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
以下の案件に応募すべきか評価し、JSONのみ返してください。

【担当エンジニアのスキル】
{MY_PROFILE}

【案件情報】
タイトル: {title}
予算: ¥{budget:,}
説明: {description[:1000]}

【評価基準】
- スキルと合致しているか
- 予算が妥当か（時間見積もり考慮）
- 難易度が高すぎないか（初案件でも対応可能か）
- 詐欺・悪質案件でないか

返すJSONの形式（前置き・後置き不要、JSONのみ）:
{{
  "score": 1〜10の整数（10が最適合）,
  "should_apply": true か false,
  "reason": "判断理由を1文で",
  "estimate_hours": 予想作業時間（整数）,
  "proposal": "提案文400字程度（です・ます調、クライアントにそのまま送れる完成形。自己紹介・実績・具体的な進め方を含める）"
}}"""

    try:
        resp = _model.generate_content(prompt)
        text = resp.text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [Gemini error] {e}")
    return {"score": 0, "should_apply": False, "reason": "評価失敗", "proposal": "", "estimate_hours": 0}


# ─── Discord 通知 ──────────────────────────────────────────────────────
async def notify(job: dict, ev: dict, client: httpx.AsyncClient):
    score = ev.get("score", 0)
    hours = ev.get("estimate_hours", 0)
    hourly = job["budget"] // hours if hours > 0 else 0
    emoji  = "🟢" if score >= 7 else "🟡"
    label  = "【即応募】" if ev.get("should_apply") and score >= 8 else "【応募推奨】" if ev.get("should_apply") else "【参考】"

    msg = (
        f"{emoji} {label} **{job['platform']}**\n\n"
        f"📋 **{job['title']}**\n"
        f"💰 予算: ¥{job['budget']:,}"
        + (f"  ⏱ 推定{hours}h → 時給換算¥{hourly:,}" if hours > 0 else "")
        + f"\n⭐ 適合度: {score}/10 — {ev.get('reason', '')}\n"
        f"🔗 {job['url']}\n\n"
        f"📝 **提案文（コピペ用）**\n"
        f"```\n{ev.get('proposal', '')}\n```"
    )

    await client.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    print(f"  → Discord 送信: [{score}/10] {job['title'][:40]}")


# ─── HTML フェッチ ─────────────────────────────────────────────────────
async def fetch(client: httpx.AsyncClient, url: str) -> BeautifulSoup | None:
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200:
            return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"  [fetch error] {url[:60]}: {e}")
    return None


# ─── 予算パース ────────────────────────────────────────────────────────
def parse_budget(text: str) -> int:
    nums = re.findall(r"[\d,]+", text.replace("，", ","))
    candidates = []
    for n in nums:
        v = int(n.replace(",", ""))
        if 1_000 <= v <= 10_000_000:
            candidates.append(v)
    return max(candidates) if candidates else 0

def dedupe(jobs: list) -> list:
    seen, out = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


# ─── Lancers スクレイピング ────────────────────────────────────────────
async def scrape_lancers(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    for kw in KEYWORDS:
        url = (
            f"https://www.lancers.jp/work/search"
            f"?open=1&sort=new&keyword={kw}&work_type[]=project"
        )
        soup = await fetch(client, url)
        if not soup:
            continue

        for tag in soup.find_all("a", href=re.compile(r"/work/detail/")):
            title = tag.get_text(strip=True)
            href  = tag.get("href", "")
            if not title or len(title) < 5:
                continue
            job_url = f"https://www.lancers.jp{href}" if href.startswith("/") else href

            parent = tag.find_parent(["li", "article", "div"]) or tag.parent
            budget = parse_budget(parent.get_text() if parent else "")

            if budget < MIN_BUDGET:
                continue

            jobs.append({
                "platform": "ランサーズ",
                "title": title,
                "url": job_url,
                "budget": budget,
                "description": "",
            })

    jobs = dedupe(jobs)

    # 詳細説明を取得（上位15件）
    for job in jobs[:15]:
        soup = await fetch(client, job["url"])
        if soup:
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            job["description"] = soup.get_text(separator="\n", strip=True)[:1200]

    print(f"  [Lancers] {len(jobs)}件")
    return jobs


# ─── Crowdworks スクレイピング ─────────────────────────────────────────
async def scrape_crowdworks(client: httpx.AsyncClient) -> list[dict]:
    jobs = []
    for kw in KEYWORDS:
        url = (
            f"https://crowdworks.jp/public/jobs/search"
            f"?order=new_job&keep_search_form=true&keyword={kw}&job_type=fixed_work"
        )
        soup = await fetch(client, url)
        if not soup:
            continue

        for tag in soup.find_all("a", href=re.compile(r"/public/jobs/\d+")):
            title = tag.get_text(strip=True)
            href  = tag.get("href", "")
            if not title or len(title) < 5:
                continue
            if any(x in href for x in ["/search", "/category", "/skill"]):
                continue
            job_url = f"https://crowdworks.jp{href}" if href.startswith("/") else href

            parent = tag.find_parent(["li", "article", "div"]) or tag.parent
            budget = parse_budget(parent.get_text() if parent else "")

            if budget < MIN_BUDGET:
                continue

            jobs.append({
                "platform": "クラウドワークス",
                "title": title,
                "url": job_url,
                "budget": budget,
                "description": "",
            })

    jobs = dedupe(jobs)

    for job in jobs[:15]:
        soup = await fetch(client, job["url"])
        if soup:
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()
            job["description"] = soup.get_text(separator="\n", strip=True)[:1200]

    print(f"  [CW] {len(jobs)}件")
    return jobs


# ─── メイン ───────────────────────────────────────────────────────────
async def main():
    print("=== FREELANCE-HUNTER 起動 ===")
    seen = load_seen()

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        lancers_jobs, cw_jobs = await asyncio.gather(
            scrape_lancers(client),
            scrape_crowdworks(client),
        )
        all_jobs = dedupe(lancers_jobs + cw_jobs)
        print(f"合計: {len(all_jobs)}件 / 未通知: ", end="")

        new_jobs = [j for j in all_jobs if j["url"] not in seen]
        print(f"{len(new_jobs)}件")

        notified = 0
        for job in new_jobs:
            ev = evaluate(job["title"], job["description"], job["budget"])
            seen.add(job["url"])

            if ev.get("score", 0) >= 6:
                await notify(job, ev, client)
                notified += 1
                await asyncio.sleep(1)  # Discord レート制限回避

        save_seen(seen)
        print(f"通知: {notified}件 / 完了")


if __name__ == "__main__":
    asyncio.run(main())
