#!/usr/bin/env python3
"""
台灣高股息ETF 每日資料自動更新腳本
Daily ETF Data Updater — runs via GitHub Actions

功能:
  1. 從 Yahoo Finance 抓取最新股價
  2. 從 MoneyDJ 爬取最新配息記錄（近8期）
  3. 重新計算年化殖利率
  4. 更新 index.html 中的資料區塊
  5. 更新 LAST_UPDATED 日期戳

執行方式:
  pip install requests beautifulsoup4 lxml
  python update_etf_data.py
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
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    'Referer': 'https://www.moneydj.com/',
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


# ── 資料抓取 ─────────────────────────────────────────────────

def fetch_yahoo_prices(etf_ids):
    """從 Yahoo Finance 抓取最新股價"""
    symbols = ','.join(f'{i}.TW' for i in etf_ids)
    url = f'https://query1.finance.yahoo.com/v8/finance/quote?symbols={symbols}'
    prices = {}
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        results = resp.json().get('quoteResponse', {}).get('result', [])
        for q in results:
            etf_id = q['symbol'].replace('.TW', '')
            prices[etf_id] = {
                'price': round(q.get('regularMarketPrice', 0), 2),
                'change_pct': round(q.get('regularMarketChangePercent', 0), 2),
            }
        log(f'  ✅ Yahoo Finance: 抓到 {len(prices)} 支 ETF 股價')
    except Exception as e:
        log(f'  ⚠️  Yahoo Finance 失敗: {e}')
    return prices


def fetch_moneydj_dividends(etf_id, max_records=8):
    """從 MoneyDJ 抓取 ETF 配息歷史"""
    url = f'https://www.moneydj.com/ETF/X/Basic/Basic0005.xdjhtm?etfid={etf_id}.TW'
    try:
        session = requests.Session()
        resp = session.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'lxml')

        # 找配息記錄表格（嘗試多種選擇器）
        table = (
            soup.find('table', id='RptControl') or
            soup.find('table', class_='datalist') or
            soup.find('table', {'border': '1'}) or
            soup.find('table')
        )
        if not table:
            log(f'    找不到表格')
            return None

        rows = []
        trs = table.find_all('tr')
        for tr in trs[1:]:  # 跳過標題列
            tds = tr.find_all('td')
            if len(tds) < 4:
                continue
            try:
                base_date = tds[0].text.strip()   # 配息基準日
                pay_date  = tds[2].text.strip()    # 發放日
                amount_raw = tds[3].text.strip()   # 配息金額
                amount = safe_float(amount_raw)
                if amount is None or amount <= 0:
                    continue

                # 建立標籤：優先使用發放日 YYYY/MM
                raw = pay_date if '/' in pay_date else base_date
                label = raw[:7] if len(raw) >= 7 else raw

                rows.append({'label': label, 'amount': amount})
                if len(rows) >= max_records:
                    break
            except Exception:
                continue

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
        divs = data.get('dividends') or []
        price = data.get('price', 0)
        y = data.get('yield', 0)

        # JSON 序列化配息資料
        divs_json = json.dumps(divs, ensure_ascii=False, separators=(',', ':'))
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

    # 替換 <<AUTO_UPDATE_START>> ... <<AUTO_UPDATE_END>> 區段
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

    # 1. 抓取股價
    log('📈 Step 1: 抓取 Yahoo Finance 股價...')
    prices = fetch_yahoo_prices(etf_ids)

    # 2. 抓取配息
    all_data = {}
    log('\n💰 Step 2: 抓取 MoneyDJ 配息記錄...')
    for etf_id, meta in ETF_META.items():
        log(f'  [{etf_id}] {meta["name"]}...')
        divs = fetch_moneydj_dividends(etf_id)

        price = prices.get(etf_id, {}).get('price', 0)
        y = calc_yield(divs, price, meta['frequency']) if divs and price else 0

        if divs:
            log(f'    ✅ 取得 {len(divs)} 筆，最新: ${divs[0]["amount"]} ({divs[0]["label"]})')
        else:
            log(f'    ⚠️  配息抓取失敗，保留舊資料（需手動更新）')

        all_data[etf_id] = {
            'dividends': divs,
            'price': price,
            'yield': y,
        }
        time.sleep(1.2)  # 避免請求過於頻繁

    # 3. 更新 HTML
    log(f'\n📝 Step 3: 更新 {HTML_PATH}...')
    success = update_html(HTML_PATH, all_data, today)

    # 4. 結果摘要
    log('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━')
    log('📊 更新摘要:')
    for etf_id, data in all_data.items():
        meta = ETF_META[etf_id]
        p = data['price']
        y = data['yield']
        d_ok = '✅' if data['dividends'] else '⚠️ '
        log(f'  {d_ok} {etf_id} {meta["name"][:8]}: 股價=${p}, 殖利率={y}%')

    if success:
        log(f'\n✅ 完成！index.html 已更新至 {today}')
    else:
        log('\n❌ 更新失敗，請檢查 HTML 格式')
        exit(1)


if __name__ == '__main__':
    main()
