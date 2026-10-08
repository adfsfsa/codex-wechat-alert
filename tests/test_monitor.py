import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import monitor as m

NOW = datetime(2026, 10, 8, 6, tzinfo=timezone.utc)
CONFIG = {'sources': [{'name': 'news', 'type': 'rss', 'url': 'https://openai.com/news/rss.xml'}]}
TEST_WORK = Path('work/tests').resolve()
TEST_WORK.mkdir(parents=True, exist_ok=True)


def item(body='We will reset Codex usage limits for all users tomorrow.', age=0):
    return {'title': 'Codex announcement', 'body': body, 'source': 'news',
            'url': 'https://openai.com/index/example/', 'published': m.stamp(NOW - timedelta(days=age))}


def state():
    return {'version': 1, 'sources': {}, 'seen': {}, 'pending': [], 'budget': {}}


class MonitorTests(unittest.TestCase):
    def run_items(self, s, items, push=lambda *a: None, dry=False):
        with patch.object(m, 'fetch', return_value=''), patch.object(m, 'parse_source', return_value=items):
            return m.run(CONFIG, s, NOW, push, dry)

    def test_classification(self):
        cases = [
            ('We will reset Codex usage limits for all users tomorrow.', True),
            ('Codex 额度明天统一重置，适用于所有用户。', True),
            ('Your Codex usage limits reset every week.', False),
            ('Codex rate limits automatically refresh every 5 hours.', False),
            ('Codex password reset is available. See documentation for usage limits.' + 'x' * 150, False),
            ('Codex has new features. No change to usage limits.', False),
            ('We will not reset Codex usage limits tomorrow.', False),
            ('Codex 明天不会重置额度。', False),
            ('We have reset ChatGPT usage limits for everyone.', False),
            ('Codex: We reset weekly limits for everyone today.', True),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(bool(m.classify(item(text))), expected)

    def test_baseline_then_new_then_duplicate(self):
        sent = []
        s, _, _ = self.run_items(state(), [item()], lambda *a: sent.append(a))
        self.assertEqual(sent, [])
        newer = item('We will reset all users\' Codex usage limits today.')
        s, _, _ = self.run_items(s, [newer], lambda *a: sent.append(a))
        self.assertEqual(len(sent), 1)
        s, _, _ = self.run_items(s, [newer], lambda *a: sent.append(a))
        self.assertEqual(len(sent), 1)

    def test_old_messages_ignored(self):
        s, _, _ = self.run_items(state(), [])
        sent = []
        self.run_items(s, [item(age=30)], lambda *a: sent.append(a))
        self.assertEqual(sent, [])

    def test_daily_cap_includes_failures_and_test(self):
        s = state()
        for _ in range(4):
            self.assertTrue(m.spend(s, NOW))
        self.assertFalse(m.spend(s, NOW))
        self.assertTrue(m.spend(s, NOW + timedelta(days=1)))

    def test_push_failure_retains_queue_and_budget(self):
        s, _, _ = self.run_items(state(), [])
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as d:
            path = Path(d) / 'state.json'
            with patch.object(m, 'fetch', return_value=''), patch.object(m, 'parse_source', return_value=[item()]):
                with self.assertRaises(RuntimeError):
                    m.execute(CONFIG, s, NOW, lambda *a: (_ for _ in ()).throw(RuntimeError('delivery error')), False, path)
            saved = m.load_state(path)
            self.assertEqual(len(saved['pending']), 1)
            self.assertEqual(saved['budget']['attempts'], 1)

    def test_dry_run_does_not_write_or_send(self):
        s, _, _ = self.run_items(state(), [])
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as d:
            path = Path(d) / 'state.json'
            with patch.object(m, 'fetch', return_value=''), patch.object(m, 'parse_source', return_value=[item()]):
                with patch.object(m, 'send') as sender:
                    m.execute(CONFIG, s, NOW, sender, True, path)
                    sender.assert_not_called()
            self.assertFalse(path.exists())

    def test_health_alert_after_three_failures_and_daily_throttle(self):
        s, sent = state(), []
        with patch.object(m, 'fetch', side_effect=ValueError('broken')):
            for _ in range(4):
                s, _, failed = m.run(CONFIG, s, NOW, lambda *a: sent.append(a))
                self.assertTrue(failed)
        self.assertEqual(len(sent), 1)

    def test_recovered_source_gets_its_own_baseline(self):
        s = state()
        s['sources']['other'] = {'initialized': True, 'failures': 0}
        sent = []
        s, _, _ = self.run_items(s, [item()], lambda *a: sent.append(a))
        self.assertEqual(sent, [])
        self.assertTrue(s['sources']['news']['initialized'])

    def test_rss_and_html_parsers(self):
        raw = '''<rss><channel><item><title>Codex</title><link>https://openai.com/index/example/</link>
        <pubDate>Thu, 08 Oct 2026 00:00:00 GMT</pubDate>
        <description>&lt;p&gt;We will reset all Codex usage limits.&lt;/p&gt;</description></item></channel></rss>'''
        self.assertEqual(len(m.parse_source(CONFIG['sources'][0], raw)), 1)
        raw = '''<time>October 8, 2026</time><h3>Codex reset<button data-anchor-id="codex-reset"></button></h3>
        <article><p>We will reset all Codex usage limits.</p><h3>Details</h3><p>Tomorrow.</p></article>'''
        source = {'name': 'log', 'type': 'changelog', 'url': 'https://learn.chatgpt.com/docs/changelog'}
        result = m.parse_source(source, raw)
        self.assertEqual(result[0]['title'], 'Codex reset')
        self.assertTrue(result[0]['url'].endswith('#codex-reset'))
        self.assertIn('Tomorrow', result[0]['body'])

    def test_empty_or_changed_source_fails_loudly(self):
        with self.assertRaises(ValueError):
            m.parse_source(CONFIG['sources'][0], '<rss><channel/></rss>')

    def test_source_host_and_state_validation(self):
        self.assertFalse(m.safe_url('https://evil.example/news'))
        self.assertFalse(m.safe_url('http://openai.com/news'))
        with tempfile.TemporaryDirectory(dir=TEST_WORK) as d:
            p = Path(d) / 'state.json'
            p.write_text('{}')
            with self.assertRaises(ValueError):
                m.load_state(p)

    def test_push_exception_does_not_leak_sendkey(self):
        with patch('urllib.request.build_opener') as factory:
            factory.return_value.open.side_effect = RuntimeError('https://sctapi.ftqq.com/SCTsecret.send')
            with self.assertRaises(RuntimeError) as caught:
                m.send('test', 'body', 'SCTsecret')
            self.assertNotIn('SCTsecret', str(caught.exception))

    def test_excess_notifications_stay_queued(self):
        s, _, _ = self.run_items(state(), [])
        items = []
        for i in range(6):
            entry = item()
            entry['url'] += str(i)
            items.append(entry)
        sent = []
        s, _, _ = self.run_items(s, items, lambda *a: sent.append(a))
        self.assertEqual(len(sent), 4)
        self.assertEqual(len(s['pending']), 2)


if __name__ == '__main__':
    unittest.main()
