"""Official announcement monitor. Python 3.11+, standard library only."""
import argparse
import copy
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, HTTPRedirectHandler
import xml.etree.ElementTree as ET

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
ALLOWED_HOSTS = {'openai.com', 'www.openai.com', 'developers.openai.com',
                 'learn.chatgpt.com', 'status.openai.com'}
MAX_BYTES = 5_000_000


def now():
    return datetime.now(UTC)


def stamp(dt):
    return dt.isoformat()


def parse_date(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, AttributeError):
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'li', 'br', 'div', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'li', 'div', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value):
    parser = Text()
    parser.feed(value)
    return '\n'.join(' '.join(x.split()) for x in ''.join(parser.parts).splitlines() if x.strip())


class Changelog(HTMLParser):
    """Each dated article is independent; never match against page navigation."""
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.items = []
        self.article = False
        self.capture = None
        self.date = ''
        self.title = ''
        self.anchor = ''
        self.parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.article:
            if tag == 'time':
                self.capture = 'date'
                self.date = ''
            elif tag == 'h3':
                self.capture = 'title'
                self.title = ''
                self.anchor = ''
            if self.capture == 'title' and attrs.get('data-anchor-id'):
                self.anchor = attrs['data-anchor-id']
            if tag == 'article':
                self.article = True
                self.capture = None
                self.parts = []
        elif tag in ('p', 'li', 'br', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self.article:
            self.parts.append(data)
        elif self.capture:
            setattr(self, self.capture, getattr(self, self.capture) + data)

    def handle_endtag(self, tag):
        if tag == 'article' and self.article:
            try:
                dt = datetime.strptime(self.date.strip(), '%B %d, %Y').replace(tzinfo=UTC)
            except ValueError:
                dt = parse_date(self.date.strip())
            if not dt or not self.anchor or not self.title.strip():
                raise ValueError('Changelog article date/title/anchor missing; parser needs update')
            self.items.append({'title': self.title.strip(), 'body': '\n'.join(
                ' '.join(p.split()) for p in ''.join(self.parts).splitlines() if p.strip()),
                'url': self.base + '#' + self.anchor, 'published': stamp(dt)})
            self.article = False
        elif not self.article and tag in ('time', 'h3'):
            self.capture = None
        elif self.article and tag in ('p', 'li', 'h2', 'h3'):
            self.parts.append('\n')


def safe_url(url):
    p = urlparse(url)
    return p.scheme == 'https' and p.hostname in ALLOWED_HOSTS and not p.username and p.port in (None, 443)


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl):
            raise ValueError('Redirect outside official source allowlist')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url):
    if not safe_url(url):
        raise ValueError('Source URL is not an allowed official HTTPS URL')
    from urllib.request import build_opener
    opener = build_opener(OfficialRedirect())
    for attempt in range(2):
        try:
            with opener.open(Request(url, headers={'User-Agent': 'CodexAnnouncementMonitor/1.0',
                              'Accept': 'application/rss+xml,application/xml,text/html'}), timeout=25) as r:
                data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError('Source exceeds size limit')
            return data.decode('utf-8-sig')
        except (URLError, TimeoutError):
            if attempt:
                raise
            time.sleep(2)


def parse_source(source, raw):
    if source['type'] == 'changelog':
        p = Changelog(source['url'])
        p.feed(raw)
        items = p.items
    elif source['type'] == 'rss':
        root = ET.fromstring(raw)
        items = []
        for node in root.findall('./channel/item'):
            bodies = [n.text or '' for n in node if n.tag in (
                'description', '{http://purl.org/rss/1.0/modules/content/}encoded')]
            items.append({'title': plain(node.findtext('title') or ''),
                          'body': plain('\n'.join(bodies)),
                          'url': node.findtext('link') or '',
                          'published': node.findtext('pubDate') or ''})
    else:
        raise ValueError('Unknown source parser')
    if not items:
        raise ValueError('No entries parsed; source layout may have changed')
    if any(not i['title'] or not safe_url(i['url']) or not parse_date(i['published']) for i in items):
        raise ValueError('Source contains invalid title, date or link')
    for item in items:
        item['source'] = source['name']
    return items


QUOTA = r'(?:rate\s*limits?|usage\s*limits?|weekly\s*limits?|quotas?|credits?|额度|限额|配额|使用限制)'
RESET = r'(?:reset\w*|replenish\w*|refresh\w*|重置|补充|恢复额度|清零)'
ROUTINE = r'(?:every\s+(?:\w+\s+){0,2}(?:week|day|hours?|monday)|each\s+(?:week|day)|automatically|rolling\s+window|每周|每天|每\s*\d+\s*小时|自动重置)'
EVENT = r'(?:we\s+(?:have\s+|are\s+|will\s+)?(?:reset|resetting|replenish)|today|tomorrow|one.time|ahead\s+of|celebrat|今天|明天|提前|本次|统一)'
GLOBAL = r'(?:all\s+(?:codex\s+)?(?:users|accounts|plans)|everyone|globally|across\s+(?:all\s+)?plans|全体|所有用户|全局|统一重置)'


def classify(item):
    text = item['title'] + '\n' + item['body']
    if not re.search(r'\bcodex\b', text, re.I):
        return None
    # Keep quota/reset words close together; a password reset elsewhere is irrelevant.
    bridge = r'[^.!?。！？\n]{0,140}?'
    pair = re.compile(QUOTA + bridge + RESET + '|' + RESET + bridge + QUOTA, re.I)
    for match in pair.finditer(text):
        context = text[max(0, match.start() - 120):match.end() + 120]
        if re.search(r'password\s+reset|reset\s+(?:your\s+)?password|密码重置|重置密码', match.group(), re.I):
            continue
        if re.search(r'\bchatgpt\b', match.group(), re.I) and not re.search(r'\bcodex\b', match.group(), re.I):
            continue
        if re.search(r'(?:will\s+not|won.t|not\s+be|no\s+plans?\s+to).{0,45}' + RESET +
                     r'|不会.{0,20}(?:重置|补充)|不.{0,8}重置', context, re.I):
            continue
        if re.search(ROUTINE, context, re.I) and not re.search(EVENT, context, re.I):
            continue
        confidence = '明确提及全局范围（规则判断）' if re.search(GLOBAL, context, re.I) else '疑似相关，范围请核对原文'
        return {'confidence': confidence, 'excerpt': context.strip()[:600]}
    return None


def identity(item):
    # Changes to an existing entry can also carry a newly added announcement.
    value = item['url'] + '\n' + item['title'] + '\n' + item['body']
    return hashlib.sha256(value.encode()).hexdigest()


def load_state(path):
    if not path.exists():
        return {'version': 1, 'sources': {}, 'seen': {}, 'pending': [], 'budget': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 1 or not all(k in state for k in ('sources', 'seen', 'pending', 'budget')):
        raise ValueError('Invalid state; restore state instead of silently rebuilding baseline')
    return state


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def send(title, body, key):
    if not re.fullmatch(r'SCT[A-Za-z0-9]+', key or ''):
        raise ValueError('SERVERCHAN_SENDKEY must be a Turbo SCT SendKey')
    data = urlencode({'title': title[:32], 'desp': body}).encode()
    # No automatic retry: delivery can succeed even when the response is lost.
    from urllib.request import build_opener, HTTPRedirectHandler
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with build_opener(NoRedirect()).open(Request('https://sctapi.ftqq.com/' + key + '.send',
                                                  data=data, method='POST'), timeout=25) as r:
            result = json.loads(r.read(100_000))
        if result.get('code') != 0:
            raise RuntimeError('Push service did not acknowledge success')
    except Exception:
        # Exception URLs may contain the secret; never expose original exception.
        raise RuntimeError('微信推送未确认成功；请检查 Server酱控制台和额度') from None


def spend(state, current):
    day = current.astimezone(CST).date().isoformat()
    if state['budget'].get('date') != day:
        state['budget'] = {'date': day, 'attempts': 0}
    if state['budget']['attempts'] >= 4:
        return False
    state['budget']['attempts'] += 1
    return True


def run(config, state, current, push, dry_run=False, checkpoint=lambda state: None):
    failures = []
    reports = []
    for source in config['sources']:
        info = state['sources'].setdefault(source['name'], {'initialized': False, 'failures': 0})
        try:
            items = parse_source(source, fetch(source['url']))
            reports.append({'source': source['name'], 'entries': len(items), 'status': 'ok',
                            'baseline': not info['initialized']})
            for item in items:
                uid = identity(item)
                if uid in state['seen']:
                    continue
                state['seen'][uid] = stamp(current)
                date = parse_date(item['published'])
                result = classify(item)
                # First successful fetch for EACH source establishes a baseline, even
                # when another source has already been running for days.
                if info['initialized'] and result and current - timedelta(days=7) <= date <= current + timedelta(days=1):
                    state['pending'].append({'id': uid, 'item': item, 'result': result,
                                             'queued': stamp(current)})
            info.update(initialized=True, failures=0, last_success=stamp(current))
        except Exception as exc:
            info['failures'] += 1
            failures.append(source['name'])
            reports.append({'source': source['name'], 'status': 'error',
                            'error_type': type(exc).__name__, 'consecutive_failures': info['failures']})
    for entry in list(state['pending']):
        if current - parse_date(entry['queued']) > timedelta(days=7):
            state['pending'].remove(entry)
            continue
        item, result = entry['item'], entry['result']
        reports.append({'candidate': item['title'], 'url': item['url'], **result})
        if dry_run:
            continue
        if not spend(state, current):
            break
        body = (f"判断：{result['confidence']}\n\n来源：{item['source']}\n\n"
                f"原文发布时间：{item['published']}\n\n"
                f"检测时间：{current.astimezone(CST):%Y-%m-%d %H:%M} 北京时间\n\n"
                f"原文标题：{item['title']}\n\n摘录：\n\n{result['excerpt']}\n\n"
                f"[打开官方原文]({item['url']})\n\n重置时间与适用范围请以原文为准。")
        checkpoint(state)
        push('Codex 额度重置相关公告', body)
        state['pending'].remove(entry)
        checkpoint(state)
    broken = [name for name in failures if state['sources'][name]['failures'] >= 3]
    last_health = parse_date(state.get('last_health', ''))
    if broken and not dry_run and (not last_health or current - last_health >= timedelta(days=1)) and spend(state, current):
        checkpoint(state)
        push('Codex 监测源读取异常', '连续三次或以上读取失败：' + '、'.join(broken) +
             '\n\n请查看 GitHub Actions 运行摘要；当前监测覆盖不完整。')
        state['last_health'] = stamp(current)
    state['last_check'] = stamp(current)
    # A daily state change also keeps a public repository active. Hashes are kept
    # for 90 days; old entries are never eligible beyond the 7-day event window.
    state['seen'] = {k: v for k, v in state['seen'].items()
                     if parse_date(v) >= current - timedelta(days=90)}
    return state, reports, bool(failures)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--state', default='data/state.json')
    parser.add_argument('--dry-run', action='store_true', help='Read live sources without saving state or pushing')
    parser.add_argument('--test-push', action='store_true', help='Send one test message without changing baseline')
    args = parser.parse_args()
    key = os.environ.get('SERVERCHAN_SENDKEY', '')
    path = Path(args.state)
    state = load_state(path)
    if not args.dry_run and not re.fullmatch(r'SCT[A-Za-z0-9]+', key):
        raise ValueError('请先配置仓库 Secret：SERVERCHAN_SENDKEY（SCT 开头）')
    push = lambda title, body: send(title, body, key)
    if args.test_push:
        if not spend(state, now()):
            raise RuntimeError('今日程序推送上限已到（4 次，包含测试和失败尝试）')
        # Persist the attempt before contacting the service, including failures.
        save_state(path, state)
        push('Codex 微信预警测试', '微信通道已接通。这是一条测试消息，并非额度重置公告。')
        print('测试请求成功；请确认手机微信实际收到通知。')
        return
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    state, reports, failed = execute(config, state, now(), push, args.dry_run, path)
    output = json.dumps({'checked_at': state.get('last_check'), 'sources': reports,
                         'pending': len(state['pending']), 'dry_run': args.dry_run}, ensure_ascii=False, indent=2)
    print(output)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as f:
            f.write('### Codex monitor\n\n```json\n' + output + '\n```\n')
    return 1 if failed else 0


def execute(config, state, current, push, dry_run, path):
    working = copy.deepcopy(state)
    checkpoint = (lambda value: None) if dry_run else (lambda value: save_state(path, value))
    try:
        return run(config, working, current, push, dry_run, checkpoint)
    finally:
        checkpoint(working)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
