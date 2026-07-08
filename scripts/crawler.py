#!/usr/bin/env python3
"""
scripts/crawler.py — Playwright browser management + source framework
R5-1: login, refresh-cookies, status
R5-2: Source framework + Edgar13FSource + fetch-sources

SECURITY: cookie/token values are NEVER printed or logged.
"""
import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "serenity.sqlite"
PROFILE_DIR = ROOT / "data" / "browser_profile"
X_CURL_DIR = ROOT / "x_curl"

# SEC EDGAR User-Agent — required by SEC fair-access policy
EDGAR_UA = "SerenitySignal research 901126jeff901126@gmail.com"

# Known fund manager CIKs (zero-padded 10-digit)
CIK_MANAGERS = {
    "0001067983": "Berkshire Hathaway (巴菲特/波克夏)",
    "0001649339": "Scion Asset Management (Michael Burry)",
    "0001656456": "Appaloosa Management (David Tepper)",
    "0001350694": "Bridgewater Associates (Ray Dalio)",
    "0001336528": "Pershing Square (Bill Ackman)",
}

# ---------------------------------------------------------------------------
# Playwright soft-import (the whole repo keeps working when absent)
# ---------------------------------------------------------------------------

def _get_playwright():
    """Soft-import playwright. Returns sync_playwright function or None."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        return sync_playwright
    except ImportError:
        return None


def _print_install_instructions():
    print("【錯誤】找不到 Playwright 套件，請依以下步驟安裝：")
    print("  pip install playwright")
    print("  playwright install chromium")
    print("安裝完成後，再次執行此指令。")


# ---------------------------------------------------------------------------
# R5-1: Cookie rewriting helpers — preserve Windows curl ^" escaping
# ---------------------------------------------------------------------------

def _escape_val_for_windows_curl(value: str) -> str:
    """Escape a raw cookie value for insertion inside -b ^"..."^" in Windows cmd curl."""
    # Embedded double-quotes must become ^\^" (caret-escaped quote in cmd)
    return value.replace('"', r'^\^"')


def _build_cookie_string(cookies: list) -> str:
    """Build a semicolon-delimited cookie string from Playwright cookie dicts."""
    parts = []
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        if name:
            parts.append(f"{name}={_escape_val_for_windows_curl(value)}")
    return "; ".join(parts)


def _rewrite_curl_file(path: Path, cookie_str: str, ct0: str) -> bool:
    """
    Rewrite one curl file in place:
      • Replace the -b ^"<old_cookies>^" value with cookie_str
      • Replace the x-csrf-token: <old_ct0> value with ct0
    Returns True if the file changed.
    NEVER receives or logs cookie values beyond what is passed in.
    """
    text = path.read_text(encoding="utf-8")

    # -b ^"<value>^"  (may span one contiguous run — greedy inside)
    new_text = re.sub(
        r'(-b \^")(.*?)(\^")(?=\s|$)',
        lambda m: f'{m.group(1)}{cookie_str}{m.group(3)}',
        text,
    )

    # x-csrf-token: <value>^"
    new_text = re.sub(
        r'(x-csrf-token: )(.*?)(\^")(?=\s|$)',
        lambda m: f'{m.group(1)}{ct0}{m.group(3)}',
        new_text,
    )

    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        return True
    return False


# ---------------------------------------------------------------------------
# R5-1: login command
# ---------------------------------------------------------------------------

_STEALTH_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
    "--disable-dev-shm-usage",
]


def _launch_persistent(p, headless: bool):
    """
    Try system Chrome first (K-1 fix: avoids X bot-detection).
    Falls back to bundled Chromium with stealth args.
    Returns the browser context.
    """
    for channel in ("chrome", "msedge", None):
        try:
            kwargs = dict(
                user_data_dir=str(PROFILE_DIR),
                headless=headless,
                args=_STEALTH_ARGS,
            )
            if channel:
                kwargs["channel"] = channel
            ctx = p.chromium.launch_persistent_context(**kwargs)
            label = channel if channel else "Chromium"
            print(f"[crawler] 使用瀏覽器：{label}")
            return ctx
        except Exception as exc:
            label = channel if channel else "Chromium"
            print(f"[crawler] {label} 啟動失敗：{exc}，嘗試下一個...")
    raise RuntimeError("所有瀏覽器都無法啟動，請確認已安裝 Chrome/Edge 或 Playwright Chromium。")


def cmd_login():
    sync_playwright = _get_playwright()
    if sync_playwright is None:
        _print_install_instructions()
        sys.exit(1)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print("正在啟動有頭瀏覽器（優先使用系統 Chrome，K-1 修復）...")

    with sync_playwright() as p:
        browser = _launch_persistent(p, headless=False)
        page = browser.new_page()
        # Remove navigator.webdriver flag (stealth)
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page.goto("https://x.com/login")
        time.sleep(2)

        print()
        print("=" * 60)
        print("【操作說明】")
        print("1. 瀏覽器已開啟 X.com 登入頁面")
        print("2. 請在瀏覽器中手動輸入帳號密碼完成登入")
        print("3. 登入成功後，請關閉瀏覽器視窗")
        print("4. Session 資訊將自動儲存至 data/browser_profile/")
        print("5. 完成後執行：python scripts/crawler.py refresh-cookies")
        print("=" * 60)
        print()

        try:
            browser.wait_for_event("close", timeout=0)
        except Exception:
            pass

        print("瀏覽器已關閉，登入 session 已儲存。")


# ---------------------------------------------------------------------------
# R5-1: refresh-cookies command
# ---------------------------------------------------------------------------

def cmd_refresh_cookies():
    sync_playwright = _get_playwright()
    if sync_playwright is None:
        _print_install_instructions()
        sys.exit(1)

    if not PROFILE_DIR.exists():
        print("【錯誤】找不到瀏覽器設定檔，請先執行 python scripts/crawler.py login 重新登入")
        sys.exit(1)

    with sync_playwright() as p:
        browser = _launch_persistent(p, headless=True)
        page = browser.new_page()

        try:
            page.goto("https://x.com/home", timeout=30000)
            time.sleep(2)

            # Detect login state
            current_url = page.url
            if "/login" in current_url or "/i/flow/login" in current_url:
                print("【錯誤】目前未登入，請先執行 python scripts/crawler.py login 重新登入")
                browser.close()
                sys.exit(1)

            # Extra check: login form presence
            try:
                login_input = page.query_selector('input[autocomplete="username"]')
                if login_input:
                    print("【錯誤】偵測到登入表單，請先執行 python scripts/crawler.py login 重新登入")
                    browser.close()
                    sys.exit(1)
            except Exception:
                pass

            time.sleep(1)

            # Get fresh cookies for x.com
            cookies = browser.cookies(["https://x.com"])

            # Extract ct0 (CSRF token)
            ct0 = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
            if not ct0:
                print("【錯誤】找不到 ct0 cookie，可能登入已失效")
                print("請先執行 python scripts/crawler.py login 重新登入")
                browser.close()
                sys.exit(1)

            cookie_str = _build_cookie_string(cookies)

            # Rewrite all .curl files
            curl_files = list(X_CURL_DIR.glob("*.curl"))
            if not curl_files:
                print("【提示】x_curl/ 目錄中沒有 .curl 檔案，無需更新")
                browser.close()
                return

            updated = 0
            for curl_path in curl_files:
                try:
                    if _rewrite_curl_file(curl_path, cookie_str, ct0):
                        updated += 1
                        print(f"  已更新：{curl_path.name}")
                except Exception as exc:
                    print(f"  【警告】無法更新 {curl_path.name}: {exc}", file=sys.stderr)

            print(f"\n完成：共更新 {updated}/{len(curl_files)} 個 curl 檔案")
            print("（cookie 已刷新，ct0 已更新）")
            # HARD RULE: never print cookie values

        except SystemExit:
            raise
        except Exception as exc:
            print(f"【錯誤】refresh-cookies 失敗：{exc}", file=sys.stderr)
            browser.close()
            sys.exit(1)

        browser.close()


# ---------------------------------------------------------------------------
# R5-1: status command
# ---------------------------------------------------------------------------

def cmd_status():
    profile_exists = PROFILE_DIR.exists()
    print(f"設定檔目錄：{'存在' if profile_exists else '不存在'} ({PROFILE_DIR})")

    curl_files = list(X_CURL_DIR.glob("*.curl"))
    print(f"curl 檔案數量：{len(curl_files)}")

    if not profile_exists:
        print("登入狀態：未設定（請執行 python scripts/crawler.py login）")
        return

    sync_playwright = _get_playwright()
    if sync_playwright is None:
        print("Playwright 狀態：未安裝")
        _print_install_instructions()
        return

    print("正在快速檢查登入狀態（無頭模式）...")
    try:
        with sync_playwright() as p:
            browser = _launch_persistent(p, headless=True)
            page = browser.new_page()
            page.goto("https://x.com/home", timeout=20000)
            time.sleep(2)
            current_url = page.url
            cookies = browser.cookies(["https://x.com"])
            has_auth = any(c["name"] == "auth_token" for c in cookies)
            has_ct0 = any(c["name"] == "ct0" for c in cookies)
            browser.close()

        if "/login" in current_url:
            print("登入狀態：未登入（session 已失效）")
            print("建議：執行 python scripts/crawler.py login 重新登入")
        else:
            status = "已登入" if has_auth else "不確定"
            print(f"登入狀態：{status}")
            print(f"Cookie：auth_token={'✓' if has_auth else '✗'}，ct0={'✓' if has_ct0 else '✗'}")

    except Exception as exc:
        print(f"登入狀態：無法確認（{exc}）")


# ---------------------------------------------------------------------------
# R5-2: Source base class + registry
# ---------------------------------------------------------------------------

class Source(ABC):
    name: str = ""
    credibility: str = "individual"   # "official" | "aggregator" | "individual"
    requires_login: bool = False

    @abstractmethod
    def fetch(self) -> list:
        """Fetch items. Returns list of standardized item dicts."""
        ...


_SOURCE_REGISTRY: list[Source] = []


def _register(cls):
    _SOURCE_REGISTRY.append(cls())
    return cls


# ---------------------------------------------------------------------------
# R5-2: DB helpers for expert_views
# ---------------------------------------------------------------------------

def _connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript("pragma journal_mode = wal;")
    _migrate_expert_views(con)
    return con


def _migrate_expert_views(con):
    """Idempotent: create expert_views table and indexes."""
    con.execute("""
        create table if not exists expert_views (
            id           integer primary key autoincrement,
            source       text not null,
            author       text,
            title        text,
            text         text not null,
            url          text unique not null,
            published_at text,
            symbols      text,
            credibility  text not null default 'individual',
            fetched_at   text not null
        )
    """)
    con.execute("""
        create index if not exists idx_expert_views_published
            on expert_views(published_at desc)
    """)
    con.commit()


def _get_tracked_symbols(con) -> list:
    """Return all symbols present in prices or mentions tables."""
    syms: set = set()
    for tbl in ("prices", "mentions"):
        try:
            rows = con.execute(f"select distinct symbol from {tbl}").fetchall()
            syms.update(r[0] for r in rows)
        except Exception:
            pass
    return sorted(syms)


def _match_symbols(issuer_name: str, tracked: list) -> list:
    """
    Map an issuer name to tracked ticker symbols via simple name-contains heuristic.
    Returns [] when no confident match — never guesses tickers.
    """
    name_up = issuer_name.upper()
    matched = []
    for sym in tracked:
        if len(sym) >= 2 and sym in name_up:
            matched.append(sym)
    return matched[:2]  # cap to avoid false-positive spam


def _insert_items(con, items: list) -> int:
    """INSERT OR IGNORE items into expert_views. Returns count of newly inserted rows."""
    inserted = 0
    for item in items:
        try:
            con.execute(
                """insert or ignore into expert_views
                   (source, author, title, text, url, published_at,
                    symbols, credibility, fetched_at)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item.get("source", ""),
                    item.get("author"),
                    item.get("title"),
                    item.get("text", ""),
                    item.get("url", ""),
                    item.get("published_at"),
                    item.get("symbols", "[]"),
                    item.get("credibility", "individual"),
                    item.get("fetched_at", dt.datetime.now(dt.timezone.utc).isoformat()),
                ),
            )
            inserted += con.execute("select changes()").fetchone()[0]
        except sqlite3.Error as exc:
            url_snip = (item.get("url") or "")[:80]
            print(f"  [expert_views] insert error: {exc} — url={url_snip}", file=sys.stderr)
    con.commit()
    return inserted


# ---------------------------------------------------------------------------
# R5-2: EDGAR helpers
# ---------------------------------------------------------------------------

def _edgar_get(url: str, retries: int = 3) -> bytes:
    """HTTP GET from SEC EDGAR with required User-Agent and ≤10 req/s politeness."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": EDGAR_UA, "Accept": "application/json, text/xml, */*"},
    )
    last_exc: Exception = RuntimeError("no attempts")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            if exc.code in (404, 403):
                raise
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(1)
            continue
    raise last_exc


_13F_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _parse_13f_xml(xml_bytes: bytes) -> list:
    """
    Parse a 13F-HR information table XML.
    Returns list of {issuer, cusip, value_usd, shares} dicts.

    value_usd is the raw <value> field from the XML (empirically in dollars,
    despite SEC form instructions saying thousands — filers vary; we store as-is
    and display in B/M accordingly).
    Returns [] on parse failure — never fabricates data.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        print(f"  [13F XML] parse error: {exc}", file=sys.stderr)
        return []

    holdings = []
    # Strip all namespace prefixes for robust parsing regardless of prefix name
    # (some filers use xmlns="...", others use ns1:, ns0:, etc.)
    for el in root.iter():
        # Strip namespace URI from tag: "{ns}tag" → "tag"
        if el.tag.startswith("{"):
            el.tag = el.tag.split("}", 1)[1]

    for entry in root.iter("infoTable"):
        def _txt(tag):
            # Case-insensitive search among direct children
            for child in entry:
                ctag = child.tag.lower()
                if ctag == tag.lower():
                    return (child.text or "").strip()
            # Also try nested (shrsOrPrnAmt contains sshPrnamt)
            for child in entry.iter():
                ctag = child.tag.lower()
                if ctag == tag.lower():
                    return (child.text or "").strip()
            return ""

        issuer = _txt("nameOfIssuer")
        cusip = _txt("cusip")
        value_raw = _txt("value")
        shares_raw = _txt("sshPrnamt")

        if not issuer:
            continue

        value_usd: Optional[int] = None
        if value_raw:
            try:
                value_usd = int(value_raw.replace(",", ""))
                # value in XML is in dollars (empirically verified across multiple filers)
            except ValueError:
                pass

        shares: Optional[int] = None
        if shares_raw:
            try:
                shares = int(shares_raw.replace(",", ""))
            except ValueError:
                pass

        holdings.append({
            "issuer": issuer,
            "cusip": cusip,
            "value_usd": value_usd,
            "shares": shares,
        })

    return holdings


def _quarter_label(date_str: str) -> str:
    """'2026-03-31' → '2026Q1'"""
    try:
        d = dt.datetime.strptime(date_str[:10], "%Y-%m-%d")
        q = (d.month - 1) // 3 + 1
        return f"{d.year}Q{q}"
    except Exception:
        return (date_str or "?")[:7]


def _fmt_val(val_usd: Optional[int]) -> str:
    if val_usd is None:
        return ""
    if val_usd >= 1_000_000_000:
        return f"${val_usd / 1_000_000_000:.1f}B"
    if val_usd >= 1_000_000:
        return f"${val_usd / 1_000_000:.0f}M"
    return f"${val_usd:,}"


# ---------------------------------------------------------------------------
# R5-2: Edgar13FSource
# ---------------------------------------------------------------------------

@_register
class Edgar13FSource(Source):
    name = "sec_edgar_13f"
    credibility = "official"
    requires_login = False

    def fetch(self) -> list:
        items = []
        con = _connect_db()
        tracked = _get_tracked_symbols(con)
        con.close()

        for cik, manager_label in CIK_MANAGERS.items():
            try:
                batch = self._fetch_cik(cik, manager_label, tracked)
                items.extend(batch)
                time.sleep(0.5)
            except Exception as exc:
                print(f"  [Edgar13F] CIK {cik} ({manager_label}): FAILED — {exc}", file=sys.stderr)
                time.sleep(0.5)

        return items

    # ---- per-CIK logic ----

    def _fetch_cik(self, cik: str, manager_label: str, tracked: list) -> list:
        print(f"  [Edgar13F] CIK {cik} ({manager_label})...")

        # Submissions index
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        raw = _edgar_get(url)
        data = json.loads(raw)
        time.sleep(0.5)

        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        filed_dates = recent.get("filingDate", [])
        report_dates = recent.get("reportDate", [])

        filings_13f = [
            {
                "accession": accessions[i],
                "filed": filed_dates[i] if i < len(filed_dates) else "",
                "period": report_dates[i] if i < len(report_dates) else "",
            }
            for i, form in enumerate(forms)
            if form == "13F-HR" and i < len(accessions)
        ]

        if not filings_13f:
            print(f"    No 13F-HR filings found")
            return []

        # Take most recent 2
        filings_13f = filings_13f[:2]

        # Fetch holdings for each
        all_holdings = []
        cik_int = int(cik)  # strip leading zeros for URL path
        for filing in filings_13f:
            h = self._fetch_holdings(cik_int, filing)
            if h is not None:
                all_holdings.append((filing, h))
            time.sleep(0.5)

        if not all_holdings:
            return []

        if len(all_holdings) >= 2:
            (newer_f, newer_h), (older_f, older_h) = all_holdings[0], all_holdings[1]
            return self._derive_changes(cik_int, manager_label, newer_f, newer_h, older_f, older_h, tracked)
        else:
            filing, holdings = all_holdings[0]
            return self._report_top(cik_int, manager_label, filing, holdings, tracked)

    def _fetch_holdings(self, cik_int: int, filing: dict) -> Optional[list]:
        """Fetch the info-table XML for one 13F-HR filing."""
        import re as _re
        acc = filing["accession"]          # e.g. "0001193125-26-226661"
        acc_nodash = acc.replace("-", "")  # e.g. "000119312526226661"

        # 1. Parse the HTML filing index to discover document names
        index_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
            f"/{acc_nodash}/{acc}-index.htm"
        )
        try:
            idx_raw = _edgar_get(index_url)
            time.sleep(0.3)
            html = idx_raw.decode("utf-8", errors="replace")
            # Find all href links to .xml files in the filing directory (exclude xsl subdir)
            hrefs = _re.findall(
                r'href="(/Archives/edgar/data/[^"]+\.xml)"',
                html,
                _re.IGNORECASE,
            )
            for href in hrefs:
                fname = href.rsplit("/", 1)[-1].lower()
                if "primary_doc" in fname or "xslform" in href.lower():
                    continue
                xml_url = f"https://www.sec.gov{href}"
                try:
                    xml_raw = _edgar_get(xml_url)
                    time.sleep(0.3)
                    holdings = _parse_13f_xml(xml_raw)
                    if holdings:
                        return holdings
                except Exception:
                    continue
        except Exception as exc:
            print(f"    [Edgar13F] HTML index failed ({acc}): {exc}", file=sys.stderr)

        # 2. Fallback: try common filenames
        for fname in ("infotable.xml", "form13fInfoTable.xml", "informationtable.xml"):
            url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
                f"/{acc_nodash}/{fname}"
            )
            try:
                xml_raw = _edgar_get(url)
                time.sleep(0.3)
                holdings = _parse_13f_xml(xml_raw)
                if holdings:
                    return holdings
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                break
            except Exception:
                continue

        print(f"    [Edgar13F] Could not locate info-table XML for {acc}", file=sys.stderr)
        return None

    def _derive_changes(
        self, cik_int, manager_label,
        newer_f, newer_h, older_f, older_h, tracked
    ) -> list:
        """Compare two quarters → derive 新建倉/加倉/減倉/清倉 actions."""
        newer_q = _quarter_label(newer_f["period"])
        filing_url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_int:010d}&type=13F-HR&dateb=&owner=include&count=5"
        )
        now_str = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

        def _norm(name):
            return re.sub(r"\s+", " ", name.upper().strip())

        # Key by CUSIP (stable across quarters even when issuer-name formatting
        # drifts, e.g. "CHEVRON CORP" vs "CHEVRON CORPORATION"); fall back to
        # normalized name only when CUSIP is missing. Aggregate duplicate keys
        # (13F lists one row per share class / put-call bucket).
        def _key(h):
            return h.get("cusip") or _norm(h["issuer"])

        def _agg(holdings):
            out: dict = {}
            for h in holdings:
                k = _key(h)
                if k in out:
                    out[k]["value_usd"] = (out[k]["value_usd"] or 0) + (h["value_usd"] or 0)
                else:
                    out[k] = dict(h)
            return out

        newer_map = _agg(newer_h)
        older_map = _agg(older_h)
        all_keys = set(newer_map) | set(older_map)

        changes = []
        for key in all_keys:
            nw = newer_map.get(key)
            ol = older_map.get(key)
            issuer = (nw or ol)["issuer"]  # type: ignore[index]

            if nw and not ol:
                action, val = "新建倉", nw["value_usd"]
            elif ol and not nw:
                action, val = "清倉", ol["value_usd"]
            else:
                nv = (nw["value_usd"] or 0)  # type: ignore[index]
                ov = (ol["value_usd"] or 0)  # type: ignore[index]
                if nv > ov * 1.10:
                    action, val = "加倉", nw["value_usd"]  # type: ignore[index]
                elif nv < ov * 0.90:
                    action, val = "減倉", nw["value_usd"]  # type: ignore[index]
                else:
                    continue

            changes.append({
                "issuer": issuer,
                "action": action,
                "value_usd": val,
                "cusip": (nw or ol).get("cusip", ""),  # type: ignore[union-attr]
            })

        # Sort by value desc, cap at 20 rows per manager per quarter
        changes.sort(key=lambda x: x["value_usd"] or 0, reverse=True)
        changes = changes[:20]

        base_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
            f"/{newer_f['accession'].replace('-','')}/{newer_f['accession']}-index.htm"
        )
        items = []
        for ch in changes:
            issuer = ch["issuer"]
            action = ch["action"]
            cusip = ch.get("cusip", "")
            val_str = _fmt_val(ch["value_usd"])
            symbols = _match_symbols(issuer, tracked)
            text = f"{manager_label} {newer_q} {action} {issuer}"
            if val_str:
                text += f"，市值 {val_str}"
            text += "（13F 申報，45 天延遲）"
            # Append CUSIP fragment to make URL unique per holding within a filing
            frag = cusip if cusip else re.sub(r"[^A-Z0-9]", "_", issuer.upper())[:20]
            item_url = f"{base_url}#{frag}"
            items.append({
                "source": self.name,
                "author": manager_label,
                "title": f"{manager_label} 13F {newer_q} {action} {issuer}",
                "text": text,
                "url": item_url,
                "published_at": newer_f["filed"],
                "symbols": json.dumps(symbols, ensure_ascii=False),
                "credibility": self.credibility,
                "fetched_at": now_str,
            })
        return items

    def _report_top(self, cik_int, manager_label, filing, holdings, tracked) -> list:
        """Single filing available: report top 20 holdings."""
        q_label = _quarter_label(filing["period"])
        acc = filing["accession"]
        base_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}"
            f"/{acc.replace('-','')}/{acc}-index.htm"
        )
        now_str = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        top = sorted(holdings, key=lambda h: h["value_usd"] or 0, reverse=True)[:20]
        items = []
        for h in top:
            issuer = h["issuer"]
            cusip = h.get("cusip", "")
            val_str = _fmt_val(h["value_usd"])
            symbols = _match_symbols(issuer, tracked)
            text = f"{manager_label} {q_label} 持倉 {issuer}"
            if val_str:
                text += f"，市值 {val_str}"
            text += "（13F 申報，45 天延遲）"
            frag = cusip if cusip else re.sub(r"[^A-Z0-9]", "_", issuer.upper())[:20]
            item_url = f"{base_url}#{frag}"
            items.append({
                "source": self.name,
                "author": manager_label,
                "title": f"{manager_label} 13F {q_label} 持倉 {issuer}",
                "text": text,
                "url": item_url,
                "published_at": filing["filed"],
                "symbols": json.dumps(symbols, ensure_ascii=False),
                "credibility": self.credibility,
                "fetched_at": now_str,
            })
        return items


# ---------------------------------------------------------------------------
# fetch-sources command
# ---------------------------------------------------------------------------

def cmd_fetch_sources():
    con = _connect_db()
    total_new = 0

    for source in _SOURCE_REGISTRY:
        if source.requires_login:
            print(f"[fetch-sources] Skip {source.name} (requires_login=True)")
            continue
        print(f"[fetch-sources] Running {source.name}...")
        try:
            items = source.fetch()
            new_rows = _insert_items(con, items)
            total_new += new_rows
            print(f"[fetch-sources] {source.name}: {len(items)} items, {new_rows} new rows inserted")
        except Exception as exc:
            import traceback
            print(f"[fetch-sources] {source.name}: ERROR — {exc}", file=sys.stderr)
            traceback.print_exc()

    try:
        total = con.execute("select count(*) from expert_views").fetchone()[0]
        print(f"\n[fetch-sources] Done — {total_new} new rows | expert_views total: {total}")
    except Exception:
        print(f"\n[fetch-sources] Done — {total_new} new rows")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Serenity crawler — Playwright session management + source ingestion"
    )
    ap.add_argument(
        "command",
        choices=["login", "refresh-cookies", "status", "fetch-sources"],
    )
    args = ap.parse_args()

    cmd_map = {
        "login": cmd_login,
        "refresh-cookies": cmd_refresh_cookies,
        "status": cmd_status,
        "fetch-sources": cmd_fetch_sources,
    }
    cmd_map[args.command]()


if __name__ == "__main__":
    main()
