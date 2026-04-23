#!/usr/bin/env python3
"""
台灣高股息ETF 每日資料自動更新腳本
Daily ETF Data Updater — runs via GitHub Actions

修復 v2:
  - 多端點重試 Yahoo Finance (query1/query2, v8/v7)
  - yfinance 套件作為備用抓取
  - 若股價仍為 0，從現有 HTML 讀取舊股價（絕不寫入 0）
  - 配息抓取失敗時保留 HTML 中原有配息記錄（不清空）
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
    從現有 index.html 讀取目前的股價與配息記錄。
    用於：若新抓取失敗，保留原有數值，避免寫入 0 或空陣列。
    """
    current = {etf_id: {'price': 0, 'dividends': []} for etf_id in ETF_META}
    try:
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 找 AUTO_UPDATE 區段
        m = re.search(r'// <<AUTO_UPDATE_START>>(.*?)// <<AUTO_UPDATE_END>>', content, re.DOTALL)
        if not m:
            return current

        block = m.group(1)

        # 解析每一行 ETF 物件
        # 格式: {id:'00919',name:'...',price:24.2,yield:10.9,...,dividendHistory:[...]}
        for etf_id in ETF_META:
            # 取出 price 值
            price_m = re.search(rf"id:'{re.escape(etf_id)}'.*?price:([\d.]+)", block)
            if price_m:
                p = safe_float(price_m.group(1))
                if p and p > 0:
                    current[etf_id]['price'] = p

            # 取出 dividendHistory 陣列
            div_m = re.search(
                rf"id:'{re.escape(etf_id)}'.*?dividendHistory:(\[.*?\])",
                block, re.DOTALL
            )
            if div_m:
                try:
                    divs = json.loads(div_m.group(1))
                    if isinstance(divs, list) and len(divs) > 0:
                        current[etf_id]['dividends'] = divs
                except Exception:
                    pass

        log(f'  📂 從 HTML 讀取備用資料：'
            + ', '.join(f'{k}=${v["price"]}' for k, v in current.items() if v["price"] > 0))
    except Exception as e:
        log(f'  ⚠️  讀取備用資料失敗: {e}')

    return current


# ── 資料抓取 ─────────────────────────────────────────────────

def fetch_yahoo_prices(etf_ids):
    """
    從 Yahoo Finance 抓取最新股價。
    依序嘗試多個端點，任一成功即回傳。
    只記錄 price > 0 的有效結果。
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
                log(f'  ✅ Yahoo Finance 成功（{url.split("?")[0].split("/")[-2]}）: 取得 {len(prices)} 支')
                return prices
            else:
                log(f'  ↳ 回應為空，嘗試下一端點...')

        except Exception as e:
            log(f'  ↳ 失敗: {e}')
        time.sleep(0.8)

    # ── 備用：yfinance 套件 ──────────────────────────────────
    if not prices:
        log('  🔄 嘗試 yfinance 備用方案...')
        try:
            import yfinance as yf
            symbols_str = ' '.join(f'{i}.TW' for i in etf_ids)
            tickers = yf.Tickers(symbols_str)
            for etf_id in etf_ids:
                try:
                    info = tickers.tickers[f'{etf_id}.TW'].fast_info
                    price = getattr(info, 'last_price', None)
                    if price and float(price) > 0:
                        prices[etf_id] = {'price': round(float(price), 2), 'change_pct': 0}
                except Exception:
                    pass
            if prices:
                log(f'  ✅ yfinance 備用成功：取得 {len(prices)} 支')
        except ImportError:
            log('  ⚠️  yfinance 未安裝，跳過備用方案')
        except Exception as e:
            log(f'  ⚠️  yfinance 備用失敗: {e}')

    if not prices:
        log('  ❌ 所有股價端點均失敗，將保留 HTML 中原有股價')

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

        divs_json  = json.dumps(divs, ensure_ascii=False, separators=(',', ':'))
        pay_months = json.dumps(meta['payMonths'])

        lines.append(
            f"  {{id:'{etf_id}',name:'{meta['name']}',"
            f"price:{price},yield:{y},fundSize:{meta['fundSize']},"
            f"frequency:'{meta['frequency']}',payMonths:{pay_months},"
            f"color:'{meta['color']}',enabled:{meta['defaultEnabled']},"
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

    # 1. 抓取股價
    log('\n📈 Step 1: 抓取 Yahoo Finance 股價...')
    prices = fetch_yahoo_prices(etf_ids)

    # 2. 抓取配息
    all_data = {}
    log('\n💰 Step 2: 抓取 MoneyDJ 配息記錄...')
    for etf_id, meta in ETF_META.items():
        log(f'  [{etf_id}] {meta["name"]}...')
        divs = fetch_moneydj_dividends(etf_id)

        # ── 股價：新抓 → HTML 備用，絕不寫 0 ──────────────────
        price = prices.get(etf_id, {}).get('price', 0)
        if not price or price <= 0:
            price = fallback[etf_id]['price']
            if price > 0:
                log(f'    ℹ️  股價使用 HTML 備用值 ${price}')
            else:
                log(f'    ⚠️  股價完全無法取得（寫入 0，請手動修正）')

        # ── 配息：新抓 → HTML 備用，不清空歷史 ────────────────
        if divs:
            log(f'    ✅ 取得 {len(divs)} 筆配息，最新: ${divs[0]["amount"]} ({divs[0]["label"]})')
        else:
            divs = fallback[etf_id]['dividends']
            if divs:
                log(f'    ℹ️  配息使用 HTML 備用值（{len(divs)} 筆）')
            else:
                log(f'    ⚠️  配息完全無法取得')

        y = calc_yield(divs, price, meta['frequency']) if divs and price else 0

        all_data[etf_id] = {
            'dividends': divs,
            'price': price,
            'yield': y,
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
        d_ok = '✅' if data['dividends'] else '⚠️ '
        p_ok = '✅' if p > 0 else '❌'
        log(f'  {d_ok}{p_ok} {etf_id} {meta["name"][:8]}: 股價=${p}, 殖利率={y}%')

    if success:
        log(f'\n✅ 完成！index.html 已更新至 {today}')
    else:
        log('\n❌ 更新失敗，請檢查 HTML 格式')
        exit(1)


if __name__ == '__main__':
    main()
