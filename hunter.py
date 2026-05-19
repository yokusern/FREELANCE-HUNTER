#!/usr/bin/env python3
"""
FREELANCE-HUNTER
────────────────
ランサーズ・クラウドワークスを自動スキャンし、
Gemini で案件評価 + 提案文生成 → Discord 通知
"""

import asyncio
import json
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright, Page
import google.generativeai as genai
import httpx

# ─── 環境変数 ──────────────────────────────────────────────────────────
LANCERS_EMAIL    = os.environ.get("LANCERS_EMAIL", "")
LANCERS_PASSWORD = os.environ.get("LANCERS_PASSWORD", "")
CW_EMAIL         = os.environ.get("CW_EMAIL", "")
CW_PASSWORD      = os.environ.get("CW_PASSWORD", "")
DISCORD_WEBHOOK  = os.environ["DISCORD_WEBHOOK_FREELANCE"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]

# ─── 検索キーワード ────────────────────────────────────────────────────
KEYWORDS = [
    "Next.js", "React", "Python", "FastAPI",
    "AIチャットボット", "Webアプリ", "LP制作",
    "ダッシュボード", "Supabase", "管理画面",
]

# ─── フィルタ条件 ──────────────────────────────────────────────────────
MAX_APPLICANTS = 15
MIN_BUDGET     = 20_000

# ─── 自分のプロフィール（提案文生成に使用） ────────────────────────────
MY_PROFILE = """
スキル:
- Next.js / React / TypeScript（フロントエンド）
- Python / FastAPI（バックエンド API）
- AIチャットボット構築（Claude / Gemini / OpenAI API）
- Supabase / PostgreSQL（データベース設計・RLS）
- Webアプリ / 管理画面 / ダッシュボード開発
- LP制作（レスポンシブ対応）
- Webスクレイピング（Playwright / BeautifulSoup）
- Vercel / Render デプロイ・運用

実績（ポートフォリオ）:
- FX分析AIシステム（FastAPI + Next.js + Supabase + Gemini）
- AIチャットボットSaaS（データベース不要、暗号化URL方式）
- リアルタイムビジネスダッシュボード（複数API統合）

経験: Web開発 2年、AI統合案件多数
"""

# ─── 既通知済みURLを管理（同じ案件を何度も通知しない） ──────────────────
SEEN_FILE = Path("seen_jobs.json")

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))


# ─── Gemini 評価 ───────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
_model = genai.GenerativeModel("gemini-2.0-flash")

def evaluate(title: str, description: str, budget: int) -> dict:
    prompt = f"""あなたはフリーランスエンジニアのキャリアアドバイザーです。
以下の案件を評価し、JSONのみ返してください。

【私のプロフィール】
{MY_PROFILE}

【案件情報】
タイトル: {title}
予算: ¥{budget:,}
説明: {description[:800]}

返すJSONの形式:
{{
  "score": 1〜10の整数（10が最適合）,
  "should_apply": true か false,
  "reason": "判断理由を1〜2文で",
  "proposal": "提案文300字程度（です・ます調、クライアントに送れる完成形で）"
}}
JSONのみ返してください。前置き・後置き不要。"""

    try:
        resp = _model.generate_content(prompt)
        text = resp.text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"  [Gemini error] {e}")
    return {"score": 0, "should_apply": False, "reason": "評価失敗", "proposal": ""}


# ─── Discord 通知 ──────────────────────────────────────────────────────
async def notify(job: dict, ev: dict):
    score = ev.get("score", 0)
    emoji = "🟢" if score >= 7 else "🟡"
    label = "【応募推奨】" if ev.get("should_apply") else "【参考】"

    msg = (
        f"{emoji} {label} **{job['platform']}**\n\n"
        f"📋 **{job['title']}**\n"
        f"💰 予算: ¥{job.get('budget', 0):,}\n"
        f"👥 応募者: {job.get('applicants', '不明')}人\n"
        f"⭐ 適合度: {score}/10 — {ev.get('reason', '')}\n"
        f"🔗 {job['url']}\n\n"
        f"📝 **提案文（コピペ用）**\n"
        f"```\n{ev.get('proposal', '')}\n```"
    )

    async with httpx.AsyncClient() as client:
        await client.post(DISCORD_WEBHOOK, json={"content": msg})


# ─── ユーティリティ ────────────────────────────────────────────────────
def parse_budget(text: str) -> int:
    nums = re.findall(r"[\d,]+", text.replace("，", ","))
    for n in reversed(nums):
        v = int(n.replace(",", ""))
        if v >= 1000:
            return v
    return 0

def dedupe(jobs: list) -> list:
    seen, out = set(), []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            out.append(j)
    return out


# ─── Lancers スクレイピング ────────────────────────────────────────────
async def scrape_lancers(browser) -> list[dict]:
    if not LANCERS_EMAIL:
        return []

    page = await browser.new_page()
    jobs = []

    try:
        # ログイン
        await page.goto("https://www.lancers.jp/user/login", timeout=60000)
        await page.wait_for_selector(
            'input[type="email"], input[name*="email"], input[id*="email"]',
            timeout=30000
        )
        await page.fill(
            'input[type="email"], input[name*="email"], input[id*="email"]',
            LANCERS_EMAIL
        )
        await page.fill('input[type="password"]', LANCERS_PASSWORD)
        await page.click('button[type="submit"], input[type="submit"], button:has-text("ログイン")')
        await page.wait_for_load_state("networkidle", timeout=60000)
        print("  [Lancers] ログイン完了")

        for kw in KEYWORDS:
            url = (
                f"https://www.lancers.jp/work/search"
                f"?open=1&sort=new&keyword={kw}&work_type[]=project"
            )
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            # 案件カードを取得（複数セレクタ試行）
            cards = await page.query_selector_all(
                ".p-search-result__item, .work-item, [class*='search-result-item']"
            )

            for card in cards[:8]:
                try:
                    # タイトル & URL
                    link = await card.query_selector("h3 a, h2 a, a[href*='/work/detail']")
                    if not link:
                        continue
                    title = (await link.inner_text()).strip()
                    href  = await link.get_attribute("href") or ""
                    job_url = f"https://www.lancers.jp{href}" if href.startswith("/") else href
                    if not job_url:
                        continue

                    # 予算
                    budget_el = await card.query_selector(
                        "[class*='price'], [class*='budget'], [class*='reward']"
                    )
                    budget = parse_budget(await budget_el.inner_text() if budget_el else "")

                    # 応募者数
                    app_el = await card.query_selector(
                        "[class*='proposal'], [class*='applicant'], [class*='offer']"
                    )
                    applicants = 999
                    if app_el:
                        m = re.search(r"\d+", await app_el.inner_text())
                        if m:
                            applicants = int(m.group())

                    if budget < MIN_BUDGET or applicants > MAX_APPLICANTS:
                        continue

                    jobs.append({
                        "platform": "ランサーズ",
                        "title": title,
                        "url": job_url,
                        "budget": budget,
                        "applicants": applicants,
                        "description": "",
                    })
                except Exception:
                    continue

        # 各案件の詳細説明を取得
        for job in jobs:
            try:
                await page.goto(job["url"], timeout=20000)
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                desc_el = await page.query_selector(
                    "[class*='description'], [class*='detail-body'], .p-job-description"
                )
                if desc_el:
                    job["description"] = (await desc_el.inner_text())[:800]
            except Exception:
                pass

    except Exception as e:
        await page.screenshot(path="lancers_error.png")
        print(f"  [Lancers error] {e}")
    finally:
        await page.close()

    return dedupe(jobs)


# ─── Crowdworks スクレイピング ─────────────────────────────────────────
async def scrape_crowdworks(browser) -> list[dict]:
    if not CW_EMAIL:
        return []

    page = await browser.new_page()
    jobs = []

    try:
        # ログイン
        await page.goto("https://crowdworks.jp/login", timeout=60000)
        await page.wait_for_selector(
            'input[type="email"], input[name*="email"], input[id*="email"]',
            timeout=30000
        )
        await page.fill(
            'input[type="email"], input[name*="email"], input[id*="email"]',
            CW_EMAIL
        )
        await page.fill('input[type="password"]', CW_PASSWORD)
        await page.click('input[type="submit"], button[type="submit"], button:has-text("ログイン")')
        await page.wait_for_load_state("networkidle", timeout=60000)
        print("  [CW] ログイン完了")

        for kw in KEYWORDS:
            url = (
                f"https://crowdworks.jp/public/jobs/search"
                f"?order=new_job&keep_search_form=true&keyword={kw}&job_type=fixed_work"
            )
            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            cards = await page.query_selector_all(
                ".job_offer, .job-list-item, [class*='job_offer']"
            )

            for card in cards[:8]:
                try:
                    link = await card.query_selector(
                        "h3 a, h2 a, a[href*='/public/jobs/']"
                    )
                    if not link:
                        continue
                    title   = (await link.inner_text()).strip()
                    href    = await link.get_attribute("href") or ""
                    job_url = f"https://crowdworks.jp{href}" if href.startswith("/") else href
                    if not job_url:
                        continue

                    budget_el = await card.query_selector(
                        "[class*='reward'], [class*='price'], [class*='budget']"
                    )
                    budget = parse_budget(await budget_el.inner_text() if budget_el else "")

                    app_el = await card.query_selector(
                        "[class*='proposal'], [class*='applicant']"
                    )
                    applicants = 999
                    if app_el:
                        m = re.search(r"\d+", await app_el.inner_text())
                        if m:
                            applicants = int(m.group())

                    if budget < MIN_BUDGET or applicants > MAX_APPLICANTS:
                        continue

                    jobs.append({
                        "platform": "クラウドワークス",
                        "title": title,
                        "url": job_url,
                        "budget": budget,
                        "applicants": applicants,
                        "description": "",
                    })
                except Exception:
                    continue

        # 詳細説明取得
        for job in jobs:
            try:
                await page.goto(job["url"], timeout=20000)
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
                desc_el = await page.query_selector(
                    ".job_description, [class*='description'], [class*='detail']"
                )
                if desc_el:
                    job["description"] = (await desc_el.inner_text())[:800]
            except Exception:
                pass

    except Exception as e:
        await page.screenshot(path="cw_error.png")
        print(f"  [CW error] {e}")
    finally:
        await page.close()

    return dedupe(jobs)


# ─── メイン ───────────────────────────────────────────────────────────
async def main():
    print("=== FREELANCE-HUNTER 起動 ===")
    seen = load_seen()
    all_jobs: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        lancers_jobs = await scrape_lancers(browser)
        cw_jobs      = await scrape_crowdworks(browser)
        await browser.close()
        all_jobs = dedupe(lancers_jobs + cw_jobs)

    print(f"取得: {len(all_jobs)}件 → 未通知: ", end="")

    new_jobs = [j for j in all_jobs if j["url"] not in seen]
    print(f"{len(new_jobs)}件")

    notified = 0
    for job in new_jobs:
        ev = evaluate(job["title"], job["description"], job["budget"])
        score = ev.get("score", 0)
        print(f"  [{score}/10] {job['title'][:40]}")

        if score >= 6:
            await notify(job, ev)
            notified += 1

        seen.add(job["url"])

    save_seen(seen)
    print(f"通知送信: {notified}件 / スキャン完了")


if __name__ == "__main__":
    asyncio.run(main())
