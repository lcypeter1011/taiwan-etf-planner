#!/usr/bin/env python3
"""
台灣高股息ETF 每日資料自動更新腳本
Daily ETF Data Updater — runs via GitHub Actions

修復 v3 — 三層備用機制：
  1. 優先：Yahoo Finance (多端點) / MoneyDJ 即時抓取
  2. 次要：從現有 HTML 讀取上次成功寫入的值
  3. 最終：硬編碼 FALLBACK_DIVIDENDS 常數（永不清空配息）
  配息金額合理性驗證（0.01~10 TWD/unit），防止爬到錯誤欄位
"""

import requests
import re
import json
import time
from datetime import date
from bs4 import BeautifulSoup

# ── 常數設定 ─────────────────────────────────────────────────

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://finance.yahoo.com/',
    'Cache-Control': 'no-cache',
}

ETF_META = {
    '00919': {'name': '群益台灣精選高息', 'color': '#22c55e', 'fundSize': 4200, 'frequency': 'quarterly', 'payMonths': [1,4,7,10],  'defaultEnabled': 'true',  'defaultAlloc': 28},
    '0056':  {'name': '元大高股息',       'color': '#3b82f6', 'fundSize': 4600, 'frequency': 'quarterly', 'payMonths': [2,5,8,11],  'defaultEnabled': 'true',  'defaultAlloc': 30},
    '00878': {'name': '國泰永續高股息',   'color': '#f97316', 'fundSize': 5800, 'frequency': 'quarterly', 'payMonths': [3,6,9,12],  'defaultEnabled': 'true',  'defaultAlloc': 42},
    '00929': {'name': '復華台灣科技優息', 'color': '#a855f7', 'fundSize': 4000, 'frequency': 'monthly',   'payMonths': list(range(1,13)), 'defaultEnabled': 'false', 'defaultAlloc': 0},
    '00713': {'name': '元大台灣高息低波', 'color': '#ef4444', 'fundSize': 2400, 'frequency': 'quarterly', 'payMonths': [1,4,7,10],  'defaultEnabled': 'false', 'defaultAlloc': 0},
}

HTML_PATH = 'index.html'

# ── 最終備用股價（硬編碼，當所有 API 均失敗時使用，避免一直回寫舊價格）──
# 定期手動更新此區塊（用 yfinance 跑一次確認）
FALLBACK_PRICES = {
    '00919': 30.26,   # 2026-06-25
    '0056':  53.20,   # 2026-06-25
    '00878': 33.75,   # 2026-06-25
    '00929': 31.86,   # 2026-06-25
    '00713': 60.85,   # 2026-06-25
}

# ── 最終備用配息資料（硬編碼，當 MoneyDJ 和 HTML 備用均失敗時使用）──
# 更新此區塊：每次人工確認配息後手動維護
FALLBACK_DIVIDENDS = {
    '00919': [
        {'label':'2026Q1','amount':0.78},{'label':'2025Q4','amount':0.54},
        {'label':'2025Q3','amount':0.54},{'label':'2025Q2','amount':0.72},
        {'label':'2025Q1','amount':0.72},{'label':'2024Q4','amount':0.72},
        {'label':'2024Q3','amount':0.72},{'label':'2024Q2','amount':0.70},
    ],
    '0056': [
        {'label':'2026Q2','amount':1.00},{'label':'2025Q4+','amount':0.866},
        {'label':'2025Q3','amount':0.866},{'label':'2025Q2','amount':0.866},
        {'label':'2025Q1','amount':1.07},{'label':'2024Q4','amount':1.07},
        {'label':'2024Q3','amount':1.07},{'label':'2024Q2','amount':1.07},
    ],
    '00878': [
        {'label':'2026Q1','amount':0.42},{'label':'2025Q4','amount':0.40},
        {'label':'2025Q3','amount':0.40},{'label':'2025Q2','amount':0.47},
        {'label':'2025Q1','amount':0.50},{'label':'2024Q4','amount':0.55},
        {'label':'2024Q3','amount':0.55},{'label':'2024Q2','amount':0.51},
    ],
    '00929': [
        {'label':'2026/04','amount':0.13},{'label':'2026/03','amount':0.11},
        {'label':'2026/02','amount':0.10},{'label':'2026/01','amount':0.09},
        {'label':'2025/12','amount':0.09},{'label':'2025/11','amount':0.07},
        {'label':'2025/10','amount':0.07},{'label':'2025/09','amount':0.06},
    ],
    '00713': [
        {'label':'2026Q1','amount':0.78},{'label':'2025Q4','amount':0.78},
        {'label':'2025Q3','amount':0.78},{'label':'2025Q2','amount':1.10},
        {'label':'2025Q1','amount':1.40},{'label':'2024Q4','amount':1.40},
        {'label':'2024Q3','amount':1.50},{'label':'2024Q2','amount':1.50},
    ],
}

# ── 工具函數 ─────────────────────────────────────────────────

def log(msg):
    print(msg, flush=True)


def safe_float(s):
    """安全地將字串轉為浮點數"""
    try:
        return float(re.sub(r'[^\d.]', '', str(s)))
    except (ValueError, TypeError):
        return None


# ── 從現有 HTML 讀取備用資料 ─────────────────────────────────

def get_current_data_from_html(html_path):
    """
    從現有 index.html 讀取股價、殖利率、配息記錄作為備用。
    只讀取合理的值（price>0, 0.1<yield<50, 有效配息陣列）。
    """
    current = {etf_id: {'price': 0, 'yield': 0, 'dividends': [], 'nav': 0} for etf_id in ETF_META}
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()

        m = re.search(r'// <<AUTO_UPDATE_START>>(.*?)// <<AUTO_UPDATE_END>>', content, re.DOTALL)
        if not m:
            return current

        block = m.group(1)

        for etf_id in ETF_META:
            # 讀取 price
            price_m = re.search(rf"id:'{re.escape(etf_id)}'[^}}]*?price:([\d.]+)", block)
            if price_m:
                p = safe_float(price_m.group(1))
                if p and p > 0:
                    current[etf_id]['price'] = p

            # 讀取 yield（只接受合理範圍）
            yield_m = re.search(rf"id:'{re.escape(etf_id)}'[^}}]*?yield:([\d.]+)", block)
            if yield_m:
                y = safe_float(yield_m.group(1))
                if y and 0.1 < y < 50:
                    current[etf_id]['yield'] = y

            # 讀取 nav（合理範圍：1~1000）
            nav_m = re.search(rf"id:'{re.escape(etf_id)}'[^}}]*?nav:([\d.]+)", block)
            if nav_m:
                n = safe_float(nav_m.group(1))
                if n and 1 < n < 1000:
                    current[etf_id]['nav'] = n

            # 讀取 dividendHistory（只接受每單位配息在 0.01~10 TWD 的記錄）
            div_m = re.search(
                rf"id:'{re.escape(etf_id)}'.*?dividendHistory:(\[.*?\])",
                block, re.DOTALL
            )
            if div_m:
                try:
                    divs = json.loads(div_m.group(1))
                    valid = [d for d in divs if isinstance(d.get('amount'), (int, float))
                             and 0.01 <= d['amount'] <= 10]
                    if valid:
                        current[etf_id]['dividends'] = valid
                except Exception:
                    pass

        log('  📂 HTML 備用資料：'
            + ', '.join(f"{k} ${v['price']} y={v['yield']}% d={len(v['dividends'])}"
                        for k, v in current.items()))
    except Exception as e:
        log(f'  ⚠️  讀取備用資料失敗: {e}')

    return current


# ── 資料抓取 ─────────────────────────────────────────────────

def fetch_twse_openapi_prices(etf_ids):
    """
    TWSE 官方 Open Data API — 專為自動化存取設計，無 IP 限制。
    回傳當日或最近一個交易日的收盤價。
    https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d
    """
    url = 'https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_d'
    prices = {}
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0',
                                          'Accept': 'application/json'}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        # 建立 {股票代號: 收盤價} 查詢表
        price_map = {}
        for row in data:
            code = row.get('Code', '').strip()
            close = safe_float(row.get('ClosingPrice', '') or row.get('殖利率(%)', ''))
            # 嘗試各種可能的收盤價欄位名稱
            for field in ('ClosePrice', 'ClosingPrice', '收盤價', 'closing_price'):
                val = safe_float(row.get(field, ''))
                if val and val > 0:
                    price_map[code] = val
                    break
        for etf_id in etf_ids:
            if etf_id in price_map and price_map[etf_id] > 0:
                prices[etf_id] = {'price': round(price_map[etf_id], 2), 'change_pct': 0}
        if prices:
            log(f'  ✅ TWSE OpenAPI 成功：取得 {len(prices)} 支 → ' +
                ', '.join(f'{k}=${v["price"]}' for k, v in prices.items()))
        else:
            log(f'  ↳ TWSE OpenAPI 回應中找不到目標 ETF（可能非交易日或欄位格式變更）')
            # Debug: 印出第一筆資料的 key，方便診斷
            if data:
                log(f'  ↳ 回應欄位：{list(data[0].keys())}')
    except Exception as e:
        log(f'  ↳ TWSE OpenAPI 失敗: {e}')
    return prices


def fetch_twse_mis_prices(etf_ids):
    """
    台灣證交所即時 API（mis.twse.com.tw）。
    盤中回傳最新成交價，盤前/盤後回傳昨日收盤。
    注意：此 API 在 GitHub Actions 環境可能因 IP 被 403。
    """
    ex_ch = '|'.join(f'tse_{i}.TW' for i in etf_ids)
    url = f'https://mis.twse.com.tw/stock/api/getStockInfo.asp?json=1&delay=0&ex_ch={ex_ch}'
    prices = {}
    try:
        resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0',
                                          'Referer': 'https://mis.twse.com.tw/'}, timeout=15)
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        data = resp.json()
        for item in data.get('msgArray', []):
            code = item.get('c', '').strip()
            if not code:
                continue
            z = item.get('z', '-')
            y_close = item.get('y', '0')
            price_str = z if z not in ['-', '0', '', None] else y_close
            price = safe_float(price_str)
            if price and price > 0:
                prices[code] = {'price': round(price, 2), 'change_pct': 0}
        if prices:
            log(f'  ✅ TWSE MIS 成功：取得 {len(prices)} 支 → ' +
                ', '.join(f'{k}=${v["price"]}' for k, v in prices.items()))
        else:
            log(f'  ↳ TWSE MIS 回應為空或全為盤中休市符號（"-"）')
    except Exception as e:
        log(f'  ↳ TWSE MIS 失敗: {e}')
    return prices


def fetch_yfinance_prices(etf_ids):
    """
    yfinance 套件抓取近期收盤價（history 方式，比 fast_info 更穩定）。
    """
    prices = {}
    try:
        import yfinance as yf
        for etf_id in etf_ids:
            try:
                ticker = yf.Ticker(f'{etf_id}.TW')
                hist = ticker.history(period='5d')
                if not hist.empty:
                    price = float(hist['Close'].iloc[-1])
                    if price > 0:
                        prices[etf_id] = {'price': round(price, 2), 'change_pct': 0}
            except Exception as e:
                log(f'    [{etf_id}] yfinance 失敗: {e}')
            time.sleep(0.5)
        if prices:
            log(f'  ✅ yfinance 成功：取得 {len(prices)} 支 → ' +
                ', '.join(f'{k}=${v["price"]}' for k, v in prices.items()))
        else:
            log(f'  ↳ yfinance 全部失敗')
    except ImportError:
        log('  ⚠️  yfinance 未安裝')
    except Exception as e:
        log(f'  ⚠️  yfinance 失敗: {e}')
    return prices


def fetch_nav(etf_ids):
    """
    從 yfinance 的 info.navPrice 欄位抓取每單位淨值（NAV）。
    navPrice 為 Yahoo Finance 提供的最近一次淨值，盤外時間也有資料。
    回傳 {etf_id: nav_price} dict。
    """
    navs = {}
    try:
        import yfinance as yf
        for etf_id in etf_ids:
            try:
                info = yf.Ticker(f'{etf_id}.TW').info
                nav = info.get('navPrice')
                if nav and float(nav) > 0:
                    navs[etf_id] = round(float(nav), 2)
            except Exception as e:
                log(f'    [{etf_id}] NAV 抓取失敗: {e}')
            time.sleep(0.3)
        if navs:
            log(f'  ✅ NAV 成功：' + ', '.join(f'{k}=${v}' for k, v in navs.items()))
        else:
            log(f'  ↳ NAV 全部失敗，將保留 HTML 中原有 NAV')
    except Exception as e:
        log(f'  ⚠️  NAV 抓取例外: {e}')
    return navs


def fetch_yahoo_api_prices(etf_ids):
    """
    Yahoo Finance REST API（不需 yfinance 套件）。
    依序嘗試 query1/query2 × v8/v7，任一成功即回傳。
    """
    symbols = ','.join(f'{i}.TW' for i in etf_ids)
    prices = {}

    endpoints = [
        f'https://query1.finance.yahoo.com/v8/finance/quote?symbols={symbols}&region=TW&lang=zh-TW',
        f'https://query2.finance.yahoo.com/v8/finance/quote?symbols={symbols}&region=TW&lang=zh-TW',
        f'https://query1.finance.yahoo.com/v7/finance/quote?symbols={symbols}',
        f'https://query2.finance.yahoo.com/v7/finance/quote?symbols={symbols}',
    ]

    for url in endpoints:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                log(f'  ↳ HTTP {resp.status_code}，嘗試下一端點...')
                continue
            results = resp.json().get('quoteResponse', {}).get('result', [])
            for q in results:
                raw_price = q.get('regularMarketPrice')
                if raw_price and float(raw_price) > 0:
                    etf_id = q['symbol'].replace('.TW', '')
                    prices[etf_id] = {
                        'price': round(float(raw_price), 2),
                        'change_pct': round(q.get('regularMarketChangePercent', 0), 2),
                    }
            if prices:
                log(f'  ✅ Yahoo API 成功（{url.split("?")[0].split("/")[-2]}）: 取得 {len(prices)} 支')
                return prices
            else:
                log(f'  ↳ 回應為空，嘗試下一端點...')
        except Exception as e:
            log(f'  ↳ 失敗: {e}')
        time.sleep(0.8)

    return prices


def fetch_all_prices(etf_ids):
    """
    依序嘗試所有價格來源，回傳第一個成功的結果。
    順序：yfinance(history) → TWSE MIS → Yahoo API → TWSE OpenAPI(個股用) → FALLBACK_PRICES

    注意：TWSE OpenAPI BWIBBU_d 端點只包含個股（不含 ETF），故排在最後。
    yfinance history() 在 GitHub Actions 環境最穩定，優先使用。
    """
    prices = {}

    # 1. yfinance（.history() 方式 — GitHub Actions 最穩定）
    log('  [1a] yfinance（history 方式，最可靠）...')
    prices = fetch_yfinance_prices(etf_ids)
    if len(prices) == len(etf_ids):
        return prices

    # 2. TWSE MIS 即時 API
    log('  [1b] TWSE MIS 即時 API...')
    prices2 = fetch_twse_mis_prices(etf_ids)
    for k, v in prices2.items():
        if k not in prices:
            prices[k] = v
    if len(prices) == len(etf_ids):
        return prices

    # 3. Yahoo Finance REST API
    log('  [1c] Yahoo Finance REST API...')
    prices3 = fetch_yahoo_api_prices(etf_ids)
    for k, v in prices3.items():
        if k not in prices:
            prices[k] = v
    if len(prices) == len(etf_ids):
        return prices

    # 4. TWSE Open Data API（注意：BWIBBU_d 只含個股，若 ETF 不在其中則補 0）
    log('  [1d] TWSE Open Data API（個股優先，ETF 可能無資料）...')
    prices4 = fetch_twse_openapi_prices(etf_ids)
    for k, v in prices4.items():
        if k not in prices:
            prices[k] = v

    if prices:
        return prices

    # 5. 硬編碼備用價格（最後防線，防止寫回舊的 HTML 價格）
    log('  [1e] 所有 API 失敗，使用硬編碼備用股價...')
    for etf_id in etf_ids:
        if etf_id not in prices and etf_id in FALLBACK_PRICES:
            prices[etf_id] = {'price': FALLBACK_PRICES[etf_id], 'change_pct': 0}
            log(f'    [{etf_id}] 使用硬編碼備用價格 ${FALLBACK_PRICES[etf_id]}')

    return prices


def find_dividend_amount(tds):
    """
    從表格行的所有欄位中，智慧尋找「每單位配息金額」。
    台灣 ETF 每單位配息通常介於 0.01 ~ 10 TWD 之間。
    回傳 (amount, col_index) 或 (None, None)。
    """
    # 先嘗試 tds[3]（原始設定）
    for try_cols in ([3], [2], [4], [1]):
        for idx in try_cols:
            if idx >= len(tds):
                continue
            text = tds[idx].text.strip()
            # 排除含斜線的日期欄位
            if '/' in text or '-' in text:
                continue
            val = safe_float(text)
            # 每單位配息合理範圍：0.01 ~ 10 TWD
            if val is not None and 0.01 <= val <= 10:
                return val, idx
    return None, None


def fetch_moneydj_dividends(etf_id, max_records=8):
    """從 MoneyDJ 抓取 ETF 配息歷史（含欄位驗證防呆）"""
    url = f'https://www.moneydj.com/ETF/X/Basic/Basic0005.xdjhtm?etfid={etf_id}.TW'
    try:
        session = requests.Session()
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')

        # 優先找有 id 的配息表格，再依序 fallback
        table = (
            soup.find('table', id='RptControl') or
            soup.find('table', class_='datalist') or
            soup.find('table', {'border': '1'})
        )
        # 若找不到特定表格，從全部 table 中挑「包含小數配息數值」的
        if not table:
            for t in soup.find_all('table'):
                trs = t.find_all('tr')
                if len(trs) >= 3:
                    # 確認至少 2 行有合理配息金額
                    hits = 0
                    for tr in trs[1:4]:
                        tds = tr.find_all('td')
                        amt, _ = find_dividend_amount(tds)
                        if amt:
                            hits += 1
                    if hits >= 2:
                        table = t
                        break

        if not table:
            log(f'    找不到含有效配息數值的表格')
            return None

        rows = []
        trs = table.find_all('tr')
        for tr in trs[1:]:
            tds = tr.find_all('td')
            if len(tds) < 2:
                continue
            try:
                amount, amt_col = find_dividend_amount(tds)
                if amount is None:
                    continue

                # 取日期：優先找含 '/' 的欄位
                date_text = ''
                for td in tds:
                    txt = td.text.strip()
                    if '/' in txt and len(txt) >= 7:
                        date_text = txt
                        break
                if not date_text:
                    date_text = tds[0].text.strip()

                label = date_text[:7] if len(date_text) >= 7 else date_text

                rows.append({'label': label, 'amount': amount})
                if len(rows) >= max_records:
                    break
            except Exception:
                continue

        if rows:
            log(f'    ✅ 取得 {len(rows)} 筆，範圍 ${rows[-1]["amount"]}~${rows[0]["amount"]}')
        else:
            log(f'    ⚠️  解析結果為空')

        return rows if rows else None

    except Exception as e:
        log(f'    MoneyDJ 抓取失敗: {e}')
        return None


# ── 計算殖利率 ───────────────────────────────────────────────

def calc_yield(dividends, price, frequency):
    """根據近期配息與股價計算年化殖利率"""
    if not dividends or not price or price <= 0:
        return 0.0
    n = 3 if frequency == 'monthly' else 4
    recent = dividends[:n]
    avg = sum(d['amount'] for d in recent) / len(recent)
    per_year = 12 if frequency == 'monthly' else 4
    return round(avg * per_year / price * 100, 1)


# ── HTML 更新 ────────────────────────────────────────────────

def build_etf_db_js(all_data, today):
    """建立完整的 ETF_DB JavaScript 字串"""
    lines = [
        f'// <<AUTO_UPDATE_START>>',
        f'const LAST_UPDATED = "{today}";',
        'const ETF_DB = [',
    ]

    for etf_id, meta in ETF_META.items():
        data = all_data.get(etf_id, {})
        divs  = data.get('dividends') or []
        price = data.get('price', 0)
        y     = data.get('yield', 0)
        nav   = data.get('nav', 0)

        # ── 最終防呆：確保寫入的數值合理 ─────────────────────
        # 每單位配息必須在合理範圍（0.01~10 TWD），否則視為爬蟲錯誤，清空
        valid_divs = [d for d in divs if isinstance(d.get('amount'), (int, float))
                      and 0.01 <= d['amount'] <= 10]
        if len(valid_divs) < len(divs):
            log(f'  ⚠️  [{etf_id}] 過濾掉 {len(divs)-len(valid_divs)} 筆異常配息記錄')
            divs = valid_divs
        # 殖利率必須在 0.1%~50% 之間
        if not (0.1 <= y <= 50):
            log(f'  ⚠️  [{etf_id}] 殖利率 {y}% 異常，重新計算')
            y = calc_yield(divs, price, meta['frequency']) if divs and price else 0
        # NAV 合理性：1~1000 TWD，否則寫 0
        if not (1 < nav < 1000):
            nav = 0

        divs_json  = json.dumps(divs, ensure_ascii=False, separators=(',', ':'))
        pay_months = json.dumps(meta['payMonths'])

        lines.append(
            f"  {{id:'{etf_id}',name:'{meta['name']}',"
            f"price:{price},yield:{y},fundSize:{meta['fundSize']},"
            f"frequency:'{meta['frequency']}',payMonths:{pay_months},"
            f"color:'{meta['color']}',enabled:{meta['defaultEnabled']},"
            f"nav:{nav},"
            f"dividendHistory:{divs_json}}},"
        )

    lines += ['];', '// <<AUTO_UPDATE_END>>']
    return '\n'.join(lines)


def update_html(html_path, all_data, today):
    """更新 index.html 中的資料區段"""
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    new_block = build_etf_db_js(all_data, today)

    pattern = r'// <<AUTO_UPDATE_START>>.*?// <<AUTO_UPDATE_END>>'
    new_html, count = re.subn(pattern, new_block, html, flags=re.DOTALL)

    if count == 0:
        log('  ⚠️  找不到 AUTO_UPDATE 區段，請確認 index.html 格式正確')
        return False

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(new_html)

    log(f'  ✅ index.html 更新完成（{today}）')
    return True


# ── 主程序 ───────────────────────────────────────────────────

def main():
    today = date.today().strftime('%Y-%m-%d')
    log(f'╔══════════════════════════════════════════╗')
    log(f'  ETF 資料自動更新  {today}')
    log(f'╚══════════════════════════════════════════╝\n')

    etf_ids = list(ETF_META.keys())

    # 0. 先讀取 HTML 中的現有資料作為最終備用
    log('📂 Step 0: 讀取現有 HTML 備用資料...')
    fallback = get_current_data_from_html(HTML_PATH)

    # 1. 抓取股價（多層備用：yfinance → TWSE MIS → Yahoo API → 硬編碼）
    log('\n📈 Step 1: 抓取股價...')
    prices = fetch_all_prices(etf_ids)

    # 1b. 抓取 NAV（yfinance navPrice，盤外時間也有最近一期資料）
    log('\n📐 Step 1b: 抓取每單位淨值（NAV）...')
    navs = fetch_nav(etf_ids)

    # 2. 抓取配息
    all_data = {}
    log('\n💰 Step 2: 抓取 MoneyDJ 配息記錄...')
    for etf_id, meta in ETF_META.items():
        log(f'  [{etf_id}] {meta["name"]}...')
        divs = fetch_moneydj_dividends(etf_id)

        # ── 股價：新抓 → 硬編碼 FALLBACK_PRICES → HTML 備用（絕不寫 0）──
        price = prices.get(etf_id, {}).get('price', 0)
        if not price or price <= 0:
            # 優先使用硬編碼備用價格（比 HTML 備用更新）
            fp = FALLBACK_PRICES.get(etf_id, 0)
            html_p = fallback[etf_id]['price']
            if fp > 0:
                price = fp
                log(f'    ℹ️  股價使用硬編碼備用 ${price}（請更新 FALLBACK_PRICES）')
            elif html_p > 0:
                price = html_p
                log(f'    ℹ️  股價使用 HTML 備用值 ${price}')
            else:
                log(f'    ⚠️  股價完全無法取得（寫入 0）')

        # ── 配息：新抓 → HTML 備用 → 硬編碼常數（三層，絕不清空）
        source = 'MoneyDJ'
        if divs:
            log(f'    ✅ 取得 {len(divs)} 筆配息，最新: ${divs[0]["amount"]} ({divs[0]["label"]})')
        else:
            divs = fallback[etf_id]['dividends']
            source = 'HTML備用'
            if divs:
                log(f'    ℹ️  配息使用 HTML 備用值（{len(divs)} 筆）')
            else:
                divs = FALLBACK_DIVIDENDS.get(etf_id, [])
                source = '硬編碼常數'
                if divs:
                    log(f'    ℹ️  配息使用硬編碼常數（{len(divs)} 筆）— 請盡快更新 FALLBACK_DIVIDENDS')
                else:
                    log(f'    ❌  配息完全無法取得')

        # ── 殖利率：計算 → HTML 備用 → 根據硬編碼常數計算 ─────
        y = calc_yield(divs, price, meta['frequency']) if (divs and price) else 0
        if not (0.1 <= y <= 50):
            y = fallback[etf_id].get('yield', 0)
            if 0.1 <= y <= 50:
                log(f'    ℹ️  殖利率使用 HTML 備用值 {y}%')
            else:
                fb_divs = FALLBACK_DIVIDENDS.get(etf_id, [])
                y = calc_yield(fb_divs, price, meta['frequency']) if (fb_divs and price) else 0
                if 0.1 <= y <= 50:
                    log(f'    ℹ️  殖利率根據硬編碼常數計算 {y}%')
                else:
                    log(f'    ⚠️  殖利率無法計算，寫入 0')

        # ── NAV：新抓 → HTML 備用（保留上次的值）──────────────
        nav = navs.get(etf_id, 0)
        if not nav or nav <= 0:
            nav = fallback[etf_id].get('nav', 0)
            if nav > 0:
                log(f'    ℹ️  NAV 使用 HTML 備用值 ${nav}')

        all_data[etf_id] = {
            'dividends': divs,
            'price': price,
            'yield': y,
            'nav': nav,
            '_source': source,
        }
        time.sleep(1.2)

    # 3. 更新 HTML
    log(f'\n📝 Step 3: 更新 {HTML_PATH}...')
    success = update_html(HTML_PATH, all_data, today)

    # 4. 結果摘要
    log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    log('📊 更新摘要:')
    for etf_id, data in all_data.items():
        meta = ETF_META[etf_id]
        p    = data['price']
        y    = data['yield']
        nav  = data.get('nav', 0)
        d_ok = '✅' if data['dividends'] else '⚠️ '
        p_ok = '✅' if p > 0 else '❌'
        nav_str = f', NAV=${nav}' if nav > 0 else ', NAV=—'
        prem_str = f' ({((p-nav)/nav*100):+.1f}%)' if nav > 0 and p > 0 else ''
        log(f'  {d_ok}{p_ok} {etf_id} {meta["name"][:8]}: 股價=${p}{nav_str}{prem_str}, 殖利率={y}%')

    if success:
        log(f'\n✅ 完成！index.html 已更新至 {today}')
    else:
        log('\n❌ 更新失敗，請檢查 HTML 格式')
        exit(1)


if __name__ == '__main__':
    main()
