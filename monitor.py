"""Free hourly adoption watcher. Public pages/RSS only. Never bypass access restrictions."""
from __future__ import annotations
import hashlib
import html
import json
import os
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import smtplib
import sys
import re

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / 'data' / 'state.json'
DOC_PATH = ROOT / 'docs' / 'data.json'
NOW = datetime.now(timezone.utc).isoformat(timespec='seconds')
HEADERS = {'User-Agent': 'BostonAdoptionWatch/1.0 (personal, low-frequency public-page monitor)'}


def norm(s):
    return ' '.join((s or '').casefold().split())


def match(s, config):
    text = norm(s)
    if any(norm(x) in text for x in config['exclude_keywords']):
        return False
    return any(norm(x) in text for x in config['keywords'])


def canonical(url):
    p = urlparse(url)
    # Strip fragments/tracking queries, retain listing identifier and functional queries.
    return p._replace(fragment='', query='&'.join(q for q in p.query.split('&') if q and not q.lower().startswith(('utm_', 'fbclid=', 'gclid=')))).geturl()


def listing(source, title, url, description='', image=''):
    url = canonical(url)
    return {'id': hashlib.sha256(url.encode()).hexdigest()[:20], 'title': title.strip()[:180],
            'url': url, 'description': description.strip()[:360], 'image': image,
            'source': source, 'first_seen': NOW}


def money_from_text(text):
    """Only prices attached to a shekel symbol or labeled as a price; never infer from dates/IDs."""
    text = (text or '').replace('\u200f', '').replace('\u200e', '')
    patterns = [r'(?:מחיר\s*[:\-]?\s*)([\d,]+)\s*(?:₪|ש[״\"]?ח)?',
                r'([\d,]+)\s*(?:₪|ש[״\"]?ח)']
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            n = int(m.group(1).replace(',', ''))
            return n
    return None


def parse_ad_date(text, now=None):
    """Parse only explicit full dates, no inference from first_seen or page scrape time."""
    text = text or ''
    for regex, fmt in ((r'(?<!\d)(\d{1,2}[./-]\d{1,2}[./-]\d{4})(?!\d)', '%d/%m/%Y'),
                       (r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', '%Y-%m-%d')):
        m = re.search(regex, text)
        if not m: continue
        value = m.group(1).replace('.', '/').replace('-', '/') if fmt.startswith('%d') else m.group(1)
        try: return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).date()
        except ValueError: pass
    return None


def sale_eligibility(price, posted, config, today=None):
    """Unknown prices/dates stay visible as unverified, not incorrectly called cheap/recent."""
    sale = config.get('sale', {})
    if not sale.get('enabled', False): return False, 'sales_disabled'
    today = today or datetime.now(timezone.utc).date()
    if posted is not None:
        age = (today - posted).days
        if age > int(sale.get('lookback_days', 90)) or age < -1:
            return False, 'outside_date_window'
    elif not sale.get('include_unknown_dates', True):
        return False, 'date_unknown'
    if price is None:
        return (bool(sale.get('include_unknown_price', True)), 'price_unknown')
    if price <= int(sale.get('symbolic_price_ceiling', 10)):
        return (bool(sale.get('include_symbolic_price', True)), 'symbolic_price_unverified')
    if price > int(sale.get('max_price_ils', 4000)):
        return False, 'over_budget'
    return True, 'price_within_budget'


def mark_sale(item, price, posted, reason):
    item.update({'listing_type': 'sale', 'price_ils': price, 'price_status': reason,
                 'posted_date': posted.isoformat() if posted else None,
                 'date_status': 'verified_date' if posted else 'unknown_date',
                 'availability': 'unverified'})
    return item


def is_probably_boston(text, config):
    return any(norm(key) in norm(text) for key in config.get('keywords', []))


def fetch_homeless(source, config, session):
    """Parse individual /pets/viewad links only; ignore promoted ads and unrelated table rows."""
    response = session.get(source['url'], headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    results, visited = [], set()
    # Prefer HTML listing table rows, which contain the price and update date.
    candidates = soup.select('tr')
    for row in candidates:
        anchors = [a for a in row.select('a[href]') if re.search(r'/pets/viewad[,%2c]', a.get('href', ''), re.I)]
        if not anchors: continue
        text = row.get_text(' ', strip=True)
        if not is_probably_boston(text, config): continue
        a = anchors[0]
        url = urljoin(response.url, a.get('href'))
        if url in visited: continue
        visited.add(url)
        # Price/date live in row cells, not in arbitrary promoted snippets.
        cells = [cell.get_text(' ', strip=True) for cell in row.select('td')]
        price_cell = next((c for c in cells if '₪' in c), '')
        date_cell = next((c for c in cells if parse_ad_date(c)), '')
        price = money_from_text(price_cell)
        posted = parse_ad_date(date_cell)
        eligible, reason = sale_eligibility(price, posted, config)
        if not eligible: continue
        image = image_url_from(row, response.url)
        item = listing(source['name'], 'בוסטון טרייר — הומלס', url, text, image)
        results.append(mark_sale(item,price,posted,reason))
    # If page has 0 ads, a healthy empty page is acceptable. But if it claims ads and
    # markup has no individual links, report warning instead of falsely claiming success.
    page_text = soup.get_text(' ', strip=True)
    if not candidates or ('מודעות עדכניות' in page_text and re.search(r'\b[1-9]\d*\s+מודעות עדכניות', page_text) and not visited):
        raise ValueError('Homeless advert rows could not be verified; inspect page structure')
    return results


def fetch_findog(source, config, session):
    resp = session.get(source['url'], timeout=25, headers=HEADERS)
    resp.raise_for_status()
    if 'type=' in source['url'] and 'type=1' in str(getattr(resp, 'url', source['url'])) and 'type=1' not in source['url']:
        raise ValueError('Findog redirected adoption/rehoming page to sales; source not trusted')
    soup = BeautifulSoup(resp.text, 'html.parser')
    text = soup.get_text(' ', strip=True)
    # The website lists unrelated recommendations after the no-results message.
    if 'לא נמצאו תוצאות מתאימות' in text or 'לא נמצאו מודעות עבור' in text:
        return []
    # Only links likely to be individual listing pages. No category/article links.
    results = []
    for a in soup.select('a[href]'):
        href = urljoin(source['url'], a['href'])
        path = urlparse(href).path.lower()
        # Must be a dog advert, not a category page or article. If the site changes
        # its URL pattern, explicitly review before extending the allowlist.
        if not any(part in path for part in ('/dogs/details/', '/dogs/detail/', '/dogs/ad/', '/dogs/view/', '/dogs/advert/')):
            continue
        parent = a.find_parent(['article', 'li', 'div']) or a
        title = a.get_text(' ', strip=True) or parent.get_text(' ', strip=True)[:100]
        context = parent.get_text(' ', strip=True)[:500]
        if source.get('listing_type') == 'sale':
            if not is_probably_boston(title + ' ' + context, config): continue
            # Listing pages may omit dates/prices: retain as unverified, not 'recent'.
            price = money_from_text(context)
            posted = parse_ad_date(context)
            eligible, reason = sale_eligibility(price, posted, config)
            if not eligible: continue
        elif not match(title + ' ' + context, config):
            continue
        img = parent.find('img')
        src = urljoin(source['url'], img.get('src', '')) if img and img.get('src') else ''
        item = listing(source['name'], title, href, context, src)
        if source.get('listing_type') == 'sale': item = mark_sale(item, price, posted, reason)
        else: item['listing_type'] = 'adoption'
        results.append(item)
    return results


def image_url_from(tag, base):
    """Extract a genuine HTTPS image URL from a listing card (never arbitrary page artwork)."""
    for img in tag.select('img'):
        for key in ('data-src', 'src', 'data-original'):
            value = (img.get(key) or '').strip()
            if not value and key == 'src':
                value = (img.get('srcset') or '').split(',')[0].strip().split(' ')[0]
            if value:
                candidate = urljoin(base, value)
                if candidate.startswith('https://') and not candidate.lower().endswith('.svg'):
                    return candidate
    return ''


def fetch_yad4(source, config, session, vision=None):
    """Monitor only actual dog cards on Yad4's public DOG search, not sitewide text."""
    resp = session.get(source['url'], timeout=25, headers=HEADERS)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    links = []
    for a in soup.select('a[href]'):
        href = urljoin(resp.url, a['href'])
        p = urlparse(href)
        if p.hostname not in ('yad4.co.il', 'www.yad4.co.il') or not p.path.startswith('/animal/'):
            continue
        if not p.path[len('/animal/'):].strip('/'):
            continue
        if href not in {x[0] for x in links}:
            links.append((href, a))
    if not links:
        raise ValueError('No /animal/ links parsed: page changed or blocking; not a healthy zero-match run')
    cap = int(source.get('max_cards', 25))
    results = []
    scanned_images = 0
    for href, anchor in links[:cap]:
        card = anchor
        # Cards may contain an image nested inside the link. Avoid using document-wide text.
        label = card.get_text(' ', strip=True)[:500]
        if not label:
            parent = anchor.parent
            if parent:
                label = parent.get_text(' ', strip=True)[:500]
        if not label:
            label = 'כלב לאימוץ ביד4'
        if any(norm(word) in norm(label) for word in config.get('exclude_keywords', [])):
            continue
        image = image_url_from(card, resp.url)
        if not image and anchor.parent:
            image = image_url_from(anchor.parent, resp.url)
        text_candidate = match(label, config)
        if text_candidate:
            row = listing(source['name'], label[:95], href, label, image)
            row['match_reason'] = 'text'
            row['listing_type'] = 'adoption'
            results.append(row)
            continue
        if vision is None or not image:
            continue
        try:
            prediction = vision.score(image, session)
            scanned_images += 1
            if prediction['breed_score'] >= float(config.get('vision_min_score', .08)):
                row = listing(source['name'], label[:95], href, label, image)
                row.update(prediction)
                row['match_reason'] = 'visual_candidate'
                row['listing_type'] = 'adoption'
                results.append(row)
        except Exception as exc:
            print(f'VISION SKIP {href}: {exc}', file=sys.stderr)
    return results, {'cards_seen': len(links), 'cards_checked': min(len(links), cap), 'images_scored': scanned_images, 'image_coverage': 'partial' if vision and scanned_images == 0 else 'attempted'}


def rss_image(entry):
    for media in entry.get('media_content', []) + entry.get('media_thumbnail', []):
        if media.get('url', '').startswith('https://'):
            return media['url']
    for link in entry.get('links', []):
        if str(link.get('type', '')).startswith('image/') and link.get('href', '').startswith('https://'):
            return link['href']
    summary = BeautifulSoup(entry.get('summary', ''), 'html.parser')
    img = summary.select_one('img[src^="https://"]')
    return img.get('src', '') if img else ''


def fetch_rss(source, config, session, vision=None):
    resp = session.get(source['url'], timeout=25, headers=HEADERS)
    resp.raise_for_status()
    import feedparser
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError('Invalid RSS feed')
    results = []
    for entry in feed.entries:
        title = BeautifulSoup(entry.get('title', ''), 'html.parser').get_text(' ', strip=True)
        summary = BeautifulSoup(entry.get('summary', ''), 'html.parser').get_text(' ', strip=True)
        link = entry.get('link', '')
        if not link.startswith('https://'):
            continue
        text_match = match(title + ' ' + summary, config)
        if text_match:
            item = listing(source['name'], title, link, summary, rss_image(entry))
            item['match_reason'] = 'text'
            item['listing_type'] = 'adoption'
            results.append(item)
            continue
        if vision is None or any(norm(word) in norm(title + ' ' + summary) for word in config.get('exclude_keywords', [])):
            continue
        image_url = rss_image(entry)
        if not image_url:
            continue
        try:
            output = vision.score(image_url, session)
            # Low threshold intentionally collects candidates for manual review; NOT a probability of breed.
            if output['breed_score'] >= float(config.get('vision_min_score', 0.08)):
                item = listing(source['name'], title, link, summary, image_url)
                item.update(output)
                item['match_reason'] = 'visual_candidate'
                item['listing_type'] = 'adoption'
                results.append(item)
        except Exception as exc:
            print(f'VISION SKIP {link}: {exc}', file=sys.stderr)
    return results


def send_email(items):
    sender = os.getenv('SMTP_USER', '').strip()
    password = os.getenv('SMTP_PASSWORD', '').strip()
    to = os.getenv('ALERT_EMAIL', '').strip()
    if not all((sender, password, to)):
        print('Mail not configured: no email sent.')
        return False
    host = os.getenv('SMTP_HOST', 'smtp-relay.brevo.com')
    port = int(os.getenv('SMTP_PORT', '587'))
    msg = EmailMessage()
    msg['From'] = os.getenv('SMTP_FROM', sender).strip()
    msg['To'] = to
    msg['Subject'] = f'Boston Terrier watch: {len(items)} new possible match(es)'
    msg.set_content('Possible matches (verify availability and adoption terms):\n\n' + '\n\n'.join(
        f"{x['title']}\n{x['source']}\n{x['url']}\nType: {x.get('listing_type', 'adoption')}\nPrice (ILS): {x.get('price_ils', 'unknown')}\nPrice status: {x.get('price_status', 'not_applicable')}\nAd date: {x.get('posted_date') or 'unknown'}\n{x['description']}" for x in items))
    with smtplib.SMTP(host, port, timeout=25) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.send_message(msg)
    return True


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    config = load_json(ROOT / 'config.json', {})
    previous_exists = STATE_PATH.exists()
    state = load_json(STATE_PATH, {'seen': {}, 'history': []})
    seen = state.setdefault('seen', {})
    outcomes = []
    fresh = []
    current = []
    has_history = bool(state.get('history'))
    pending = state.setdefault('pending_email', {})
    vision = None
    with requests.Session() as session:
        for source in config['sources']:
            try:
                if source['type'] == 'findog':
                    results = fetch_findog(source, config, session)
                elif source['type'] == 'homeless_sale':
                    results = fetch_homeless(source, config, session)
                elif source['type'] == 'rss':
                    results = fetch_rss(source, config, session)
                elif source['type'] == 'rss_visual':
                    if vision is None:
                        from vision import BreedTriage
                        vision = BreedTriage()
                    results = fetch_rss(source, config, session, vision=vision)
                elif source['type'] in ('yad4', 'yad4_visual'):
                    if source['type'] == 'yad4_visual' and vision is None:
                        from vision import BreedTriage
                        vision = BreedTriage()
                    results, detail = fetch_yad4(source, config, session, vision if source['type'] == 'yad4_visual' else None)
                else:
                    raise ValueError('Unsupported source type: ' + source['type'])
                status = 'ok'
                if source['type'] == 'yad4_visual' and detail['images_scored'] == 0:
                    status = 'warning'
                    detail['detail'] = 'Cards parsed, but no images classified; visual coverage unverified'
                outcomes.append({'name': source['name'], 'status': status, 'matches': len(results), **(detail if source['type'] in ('yad4', 'yad4_visual') else {})})
                current.extend(results)
            except Exception as exc:
                outcomes.append({'name': source['name'], 'status': 'error', 'detail': str(exc)[:180]})
                print(f"ERROR {source['name']}: {exc}", file=sys.stderr)
    # Do not treat a failed scrape as a successful zero-findings run.
    unique = {item['id']: item for item in current}
    for key, item in unique.items():
        if key not in seen:
            seen[key] = item
            fresh.append(item)
    state['last_run'] = NOW
    state['last_sources'] = outcomes
    state['history'] = (state.get('history', []) + [{'at': NOW, 'new': len(fresh), 'sources_ok': sum(x['status'] == 'ok' for x in outcomes)}])[-300:]
    # Preserve unsent alerts across SMTP failures; never silently lose a discovery.
    is_first_run = not has_history
    if not is_first_run or config.get('first_run_email', False):
        for item in fresh:
            pending[item['id']] = item
    mail_error = None
    if pending:
        try:
            sent = send_email(list(pending.values())[:20])
            if sent:
                for key in list(pending)[:20]:
                    pending.pop(key, None)
                state['last_email'] = NOW
            else:
                mail_error = 'Email missing SMTP configuration; pending matches retained'
        except Exception as exc:
            mail_error = str(exc)[:180]
            print(f'EMAIL ERROR: {exc}', file=sys.stderr)
    if mail_error:
        state['email_error'] = mail_error
    else:
        state.pop('email_error', None)
    save_json(STATE_PATH, state)
    save_json(DOC_PATH, {'updated_at': NOW, 'sources': outcomes,
                         'matches': sorted(seen.values(), key=lambda x: x['first_seen'], reverse=True)[:200],
                         'recent_runs': state['history'][-30:],
                         'email_configured': bool(os.getenv('SMTP_USER') and os.getenv('SMTP_PASSWORD') and os.getenv('ALERT_EMAIL')),
                         'email_error': mail_error, 'pending_email_count': len(pending)})
    print(f"Sources: {len(outcomes)}; new matches: {len(fresh)}; retained: {len(seen)}")
    # Leave artifacts on disk for diagnosis even if a source fails.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
