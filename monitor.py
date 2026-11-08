"""Free hourly adoption watcher. Public pages/RSS only. Never bypass access restrictions."""
from __future__ import annotations
import hashlib
import html
import json
import os
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import smtplib
import sys

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


def fetch_findog(source, config, session):
    resp = session.get(source['url'], timeout=25, headers=HEADERS)
    resp.raise_for_status()
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
        if not match(title + ' ' + context, config):
            continue
        img = parent.find('img')
        src = urljoin(source['url'], img.get('src', '')) if img and img.get('src') else ''
        results.append(listing(source['name'], title, href, context, src))
    return results


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
        f"{x['title']}\n{x['source']}\n{x['url']}\n{x['description']}" for x in items))
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
    vision = None
    with requests.Session() as session:
        for source in config['sources']:
            try:
                if source['type'] == 'findog':
                    results = fetch_findog(source, config, session)
                elif source['type'] == 'rss':
                    results = fetch_rss(source, config, session)
                elif source['type'] == 'rss_visual':
                    if vision is None:
                        from vision import BreedTriage
                        vision = BreedTriage()
                    results = fetch_rss(source, config, session, vision=vision)
                else:
                    raise ValueError('Unsupported source type: ' + source['type'])
                outcomes.append({'name': source['name'], 'status': 'ok', 'matches': len(results)})
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
    # On first run, record existing listings but do not send a backlog by default.
    is_first_run = not has_history
    should_send = bool(fresh) and (not is_first_run or config.get('first_run_email', False))
    mail_error = None
    if should_send:
        try:
            sent = send_email(fresh)
            state['last_email'] = NOW if sent else state.get('last_email')
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
                         'email_error': mail_error})
    print(f"Sources: {len(outcomes)}; new matches: {len(fresh)}; retained: {len(seen)}")
    # Leave artifacts on disk for diagnosis even if a source fails.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
