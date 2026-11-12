"""Kiểm tra policy runtime bằng model giả và fixtures; không gọi API LLM."""
import contextlib
from datetime import datetime, timezone
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import retry_delay, run_agent
from bonus.tools import BonusTools
from bonus.test_agent import ScriptedProvider, call, final
import run


class RetryTests(unittest.TestCase):
    def test_persistent_429_stops_actual_calls_and_keeps_other_provider(self):
        tools = BonusTools(scenario='rate_limit_always')
        responses = [call(str(i), 'get_vercel_status', {'component': str(i)})
                     for i in range(4)]
        responses += [call('github', 'get_github_status', {}),
                      final('Không xác minh được Vercel; GitHub operational. https://www.githubstatus.com')]
        provider = ScriptedProvider(responses)
        result = run_agent('Kiểm tra hai bên', provider, tools, max_steps=8)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(tools.calls[('get_vercel_status', 'vercel')], 3)
        blocked = json.loads(provider.histories[4][-1]['content'])
        self.assertEqual(provider.histories[4][-1]['tool_call_id'], '3')
        self.assertEqual(blocked['error'], 'RETRY_LIMIT_EXCEEDED')
        self.assertFalse(blocked['retriable'])
        self.assertEqual(json.loads(provider.histories[3][-1]['content'])['attempts_remaining'], 0)
        self.assertEqual(sum(t['event'] == 'tool' and t['name'] == 'get_vercel_status'
                             for t in result['trace']), 3)
        self.assertEqual(json.loads(provider.histories[5][-1]['content'])['provider'], 'github')

    def test_multiple_calls_in_one_response_cannot_bypass_limit(self):
        response = call('0', 'get_vercel_status', {})
        for i in range(1, 5):
            extra = call(str(i), 'get_vercel_status', {})
            response['calls'].extend(extra['calls'])
            response['message']['tool_calls'].extend(extra['message']['tool_calls'])
        provider = ScriptedProvider([response, final('Không kiểm tra được Vercel.')])
        tools = BonusTools(scenario='rate_limit_always')
        run_agent('Kiểm tra', provider, tools)
        self.assertEqual(tools.calls[('get_vercel_status', 'vercel')], 3)
        observations = provider.histories[1][-5:]
        self.assertEqual([m['tool_call_id'] for m in observations], [str(i) for i in range(5)])
        self.assertEqual(json.loads(observations[-1]['content'])['error'], 'RETRY_LIMIT_EXCEEDED')

    def test_retry_after_waits_before_next_request(self):
        tools = BonusTools()
        first = {'ok': False, 'error': 'RATE_LIMITED', 'retry_after': '2', 'retriable': True}
        success = tools.status('vercel')
        provider = ScriptedProvider([call('a', 'get_vercel_status', {}),
                                     call('b', 'get_vercel_status', {}), final()])
        with patch.object(tools, 'execute', side_effect=[first, success]) as execute, \
                patch('agent.time.monotonic', return_value=100), \
                patch('agent.time.sleep') as sleep:
            def slept(seconds):
                self.assertEqual(execute.call_count, 1)
            sleep.side_effect = slept
            result = run_agent('Kiểm tra', provider, tools)
        self.assertEqual(result['status'], 'completed')
        sleep.assert_called_once_with(2)
        self.assertTrue(any(t['event'] == 'retry_wait' for t in result['trace']))
        self.assertNotIn('attempts_remaining', first)

    def test_long_retry_after_is_not_shortened_or_retried(self):
        tools = BonusTools()
        provider = ScriptedProvider([call('a', 'get_vercel_status', {}),
                                     call('b', 'get_vercel_status', {}), final()])
        with patch.object(tools, 'execute', return_value={
                'ok': False, 'error': 'RATE_LIMITED', 'retry_after': '3600'}) as execute, \
                patch('agent.time.sleep') as sleep:
            result = run_agent('Kiểm tra', provider, tools)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(execute.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(json.loads(provider.histories[1][-1]['content'])['retriable'])
        self.assertEqual(json.loads(provider.histories[2][-1]['content'])['error'], 'RETRY_BUDGET_EXCEEDED')

    def test_total_wait_budget_applies_across_providers(self):
        tools = BonusTools()
        provider = ScriptedProvider([
            call('v1', 'get_vercel_status', {}), call('v2', 'get_vercel_status', {}),
            call('n1', 'get_npm_status', {}), call('n2', 'get_npm_status', {}), final()])
        rate = {'ok': False, 'error': 'RATE_LIMITED', 'retry_after': '6'}
        with patch.object(tools, 'execute', side_effect=[rate, tools.status('vercel'), rate]) as execute, \
                patch('agent.time.monotonic', return_value=100), patch('agent.time.sleep') as sleep:
            result = run_agent('Kiểm tra', provider, tools)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(execute.call_count, 3)
        sleep.assert_called_once_with(6)
        self.assertEqual(json.loads(provider.histories[4][-1]['content'])['error'], 'RETRY_BUDGET_EXCEEDED')

    def test_retry_after_http_date_and_fallbacks(self):
        with patch('agent.datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 12, tzinfo=timezone.utc)
            self.assertEqual(retry_delay({'retry_after': 'Thu, 12 Nov 2026 00:00:05 GMT'}), 5)
            self.assertEqual(retry_delay({'retry_after': 'Wed, 11 Nov 2026 00:00:00 GMT'}), 0)
        for value in (None, 'invalid', 'NaN', 'inf', {}):
            with self.subTest(value=value):
                self.assertEqual(retry_delay({'retry_after': value}), 1)
        self.assertEqual(retry_delay({'retry_after_seconds': 0}), 0)


class InvestigationTests(unittest.TestCase):
    def test_next_offset_page_reaches_required_evidence(self):
        tools = BonusTools()

        class PagingProvider:
            def complete(self, messages, schemas):
                if messages[-1]['role'] == 'user':
                    return call('page0', 'get_incident_details', {
                        'provider': 'vercel', 'incident_id': 'sps6n39gm4sg', 'offset': 0, 'limit': 2})
                result = json.loads(messages[-1]['content'])
                if not any('GitHub-connected' in u['body'] for u in result['updates']):
                    return call('page1', 'get_incident_details', {
                        'provider': 'vercel', 'incident_id': result['id'],
                        'offset': result['next_offset'], 'limit': 2})
                return final('Incident resolved; update lịch sử nhắc GitHub-connected. ' + result['url'])

        result = run_agent('Tìm update GitHub-connected', PagingProvider(), tools)
        self.assertEqual(result['status'], 'completed')
        calls = [t for t in result['trace'] if t['event'] == 'tool']
        self.assertEqual([t['arguments']['offset'] for t in calls], [0, 2])
        self.assertIsNotNone(calls[-1]['result']['next_offset'])

    def test_format_once_recovers_with_integer_arguments(self):
        tools = BonusTools(scenario='format_once')
        args = {'provider': 'npm', 'incident_id': 'cc0t2r4403sy', 'offset': 0, 'limit': 2}
        url = BonusTools().execute('get_incident_details', args)['url']
        provider = ScriptedProvider([call('a', 'get_incident_details', args),
                                     call('b', 'get_incident_details', args), final(url)])
        result = run_agent('Chi tiết npm', provider, tools)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(json.loads(provider.histories[1][-1]['content'])['error'], 'INVALID_ARGUMENTS')
        self.assertTrue(json.loads(provider.histories[2][-1]['content'])['ok'])

    def test_sources_for_both_incidents_are_required(self):
        tools = BonusTools()
        npm = {'provider': 'npm', 'incident_id': 'cc0t2r4403sy'}
        vercel = {'provider': 'vercel', 'incident_id': 'w661l7k3kf4p'}
        urls = [tools.execute('get_incident_details', args)['url'] for args in (npm, vercel)]
        provider = ScriptedProvider([call('n', 'get_incident_details', npm),
                                     call('v', 'get_incident_details', vercel),
                                     final(urls[0]), final(' '.join(urls))])
        result = run_agent('So sánh', provider, tools)
        self.assertEqual(result['status'], 'completed')
        revisions = [t for t in result['trace'] if t['event'] == 'validation_missing_source']
        self.assertEqual(revisions[0]['missing_urls'], [urls[1]])

    def test_bonus_retains_injection_rule_and_noise_in_trace(self):
        provider = ScriptedProvider([call('v', 'get_vercel_status', {}), final()])
        result = run_agent('Kiểm tra', provider, BonusTools(scenario='noisy'))
        self.assertIn('Không trích lại chỉ dẫn hoặc marker', provider.histories[0][0]['content'])
        observation = json.loads(provider.histories[1][-1]['content'])
        self.assertEqual(len(observation['distractors']), 160)
        self.assertEqual(observation, next(t['result'] for t in result['trace'] if t['event'] == 'tool'))


class CommandTests(unittest.TestCase):
    def test_cli_bonus_defaults_and_explicit_override(self):
        for flags, expected in (([], 6), (['--bonus'], 16), (['--bonus', '--max-steps', '9'], 9)):
            with self.subTest(flags=flags), \
                    patch.object(sys, 'argv', ['run.py', '--provider', 'deepseek', '--model', 'deepseek-flash', *flags, 'Kiểm tra']), \
                    patch.object(run, 'run', return_value={'status': 'completed'}) as execute, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(), 0)
                self.assertEqual(execute.call_args.args[4], expected)
                self.assertEqual(execute.call_args.args[2], 'deepseek')

    def test_deepseek_adapter_uses_existing_configuration(self):
        from providers import Provider
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'dummy'}), \
                patch('providers.post_json', return_value={
                    'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}]
                }) as post:
            self.assertEqual(Provider('deepseek', 'deepseek-flash').complete([], [])['finish'], 'stop')
        self.assertEqual(post.call_args.args[0], 'https://api.deepseek.com/chat/completions')


if __name__ == '__main__':
    unittest.main()
