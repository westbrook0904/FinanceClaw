#!/usr/bin/env python3
"""Explicit opt-in model/MCP/Ziwei acceptance through the existing governed Java API."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlencode, urlsplit
import uuid

ZIWEI = dict(zip(('ziwei_natal_chart', 'ziwei_decadal_chart', 'ziwei_yearly_chart',
                  'ziwei_monthly_chart', 'ziwei_daily_chart'), ('本命', '大限', '流年', '流月', '流日')))


class AcceptanceFailure(ValueError):
    """Only these locally authored messages may be printed on validation failure."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ziwei_prompt(tool, clarify=False):
    sex = '排盘性别暂未确认，请先询问，不要猜测。' if clarify else '排盘性别男。'
    target = '' if tool == 'ziwei_natal_chart' else '目标日期为2026年2月17日，仅查询当天。'
    return (f'这是合成测试资料：公历1990年6月15日、寅时、上海、民用时间。{sex}'
            f'请查看{ZIWEI[tool]}盘面，{target}只展示盘面，不做运势解读，mode=chart_only，focus=overall。'
            '完成工具调用后只返回一个JSON对象，不加Markdown或其他文字，字段严格为：'
            'mode（chart_only）、artifact_id、chart_id、target（原样复制投影）、'
            'segments（逐段只保留start和end）、citations。citations至少两项且fact_id不同，'
            '包含一条本命事实及一条所查询层级的事实（查询本命时两条本命即可）。'
            '每项严格包含fact_id、path、value；path为该事实value内一个标量的JSON Pointer，'
            'value原样复制该标量。必须使用工具返回的事实ID、区间和Artifact ID，不得编造。')


def require(condition, message):
    if not condition:
        raise AcceptanceFailure(message)


def reference_natal():
    directory = Path(__file__).resolve().parents[1] / 'src/test/resources/ziwei/domain'
    data = (directory / 'domain.json').read_bytes()
    manifest = json.loads((directory / 'manifest.json').read_text())
    require(hashlib.sha256(data).hexdigest() == manifest['fixture_sha256'], 'Frozen reference changed')
    return {f['fact_id']: f for f in json.loads(data)[0]['facts'] if f['fact_id'].startswith('natal.core.')}


def pointer(value, path):
    require(isinstance(path, str) and path.startswith('/') and len(path) <= 128,
            'Invalid citation pointer')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def verify_ziwei(call, result, tool):
    """Compare scalar citations and complete interval coverage to protected, paginated artifacts."""
    answer = json.loads(result)
    require(isinstance(answer, dict) and set(answer) == {
        'mode', 'artifact_id', 'chart_id', 'target', 'segments', 'citations'}, 'Unexpected answer shape')
    require(answer['mode'] == 'chart_only', 'Expected chart_only result')
    aid = str(uuid.UUID(answer['artifact_id']))
    require(aid == answer['artifact_id'], 'Invalid artifact identity')
    artifact_hash = None

    def read(path, start=0):
        nonlocal artifact_hash
        page = call('/v1/artifacts/' + aid + '?' + urlencode({'path': path, 'start': start, 'limit': 20}))
        require(page['artifact_id'] == aid, 'Artifact identity changed')
        if artifact_hash is None:
            artifact_hash = page['hash']
        require(page['hash'] == artifact_hash, 'Artifact hash changed during verification')
        return page

    def rows(path, maximum):
        output = []
        while True:
            page = read(path, len(output))
            require(isinstance(page['data'], list) and page['start'] == len(output)
                    and page['count'] == len(page['data']) and 0 <= page['total'] <= maximum,
                    'Invalid artifact pagination')
            output.extend(page['data'])
            if len(output) == page['total']:
                return output
            require(page['count'] > 0 and len(output) < page['total'], 'Incomplete artifact pagination')

    level = tool.removeprefix('ziwei_').removesuffix('_chart')
    require(read('/level')['data'] == level, 'Wrong chart level')
    require(read('/chart_id')['data'] == answer['chart_id'], 'Wrong chart identity')
    target = read('/target')['data']
    require(target == answer['target'], 'Wrong target coverage')
    require(target is None if level == 'natal' else
            isinstance(target, dict) and target.get('start') == '2026-02-17'
            and target.get('end') == '2026-02-18', 'Target does not match the synthetic request')
    expected_segments = [{'start': s['start'], 'end': s['end']} for s in rows('/segments', 32)]
    require(answer['segments'] == expected_segments, 'Incomplete segment coverage')
    facts = {f['fact_id']: f for f in rows('/facts', 2048)}
    require({k: f for k, f in facts.items() if k.startswith('natal.core.')} == reference_natal(),
            'Birth chart does not match the frozen synthetic reference')
    citations = answer['citations']
    require(isinstance(citations, list) and 2 <= len(citations) <= 10, 'Expected two to ten citations')
    seen, layers = set(), set()
    for citation in citations:
        require(set(citation) == {'fact_id', 'path', 'value'}, 'Invalid citation shape')
        fid = citation['fact_id']
        require(fid in facts and fid not in seen, 'Unknown or duplicate fact reference')
        seen.add(fid)
        layers.add(facts[fid]['layer'])
        actual = pointer(facts[fid]['value'], citation['path'])
        require(isinstance(actual, (str, int, float, bool)) and type(actual) is type(citation['value'])
                and actual == citation['value'], 'Citation value does not match the fact')
    require({level, 'natal'} <= layers, 'Requested chart layer was not cited')
    return {'artifact_sha256': artifact_hash, 'synthetic_birth_reference_matched': True, 'verified_citations': len(seen),
            'verified_segments': len(expected_segments), 'level': level}


def run(args, call, sleep=time.sleep):
    """No request retries or new-command recovery; an uncertain test remains the original Turn."""
    prompt = ziwei_prompt(args.expected_tool, args.clarify) if args.kind == 'ziwei' else args.prompt
    start = time.monotonic()
    c = call('/v1/conversations', {})['conversation_id']
    grant = call(f'/v1/conversations/{c}/grant')
    scopes = ['ziwei:read', 'artifact:read'] if args.kind == 'ziwei' else ['catalog:read', 'artifact:read', 'travel:read']
    call(f'/v1/conversations/{c}/grant', {'revision': grant['revision'], 'scopes': scopes}, method='PUT')
    receipt = call(f'/v1/conversations/{c}/turns', {'message': prompt})
    p = f'/v1/conversations/{c}/turns/{receipt["turn_id"]}'
    # Preserve only identifiers if interrupted; never print model responses or submitted birth data.
    out = Path('target/m6' if args.kind == 'ziwei' else 'target/m5')
    out.mkdir(parents=True, exist_ok=True)
    suffix = args.expected_tool + ('-clarify' if args.clarify else '') if args.kind == 'ziwei' else args.kind
    destination = out / f'live-{suffix}.json'
    evidence = {'kind': args.kind, 'turn_id': receipt['turn_id'], 'conversation_id': c,
                'tool': args.expected_tool, 'status': 'started',
                'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
    destination.write_text(json.dumps(evidence, indent=2) + '\n')
    clarified = False
    while True:
        t = call(p)
        if t['status'] == 'waiting' and args.kind == 'ziwei' and args.clarify and not clarified:
            questions = call(p + '/interactions')
            require(len(questions) == 1, 'Unexpected clarification batch')
            q = questions[0]
            require(q['kind'] == 'input' and q['tool'] == args.expected_tool
                    and q['status'] == 'pending' and q['live'], 'Unexpected pending interaction')
            call(p + '/interactions/' + q['id'] + '/answer', {'kind': 'input', 'revision': q['revision'],
                 'decision': None, 'action_hash': None, 'answer': {'birth': {'sex_for_chart': 'male'}}})
            clarified = True
        elif t['status'] in ('completed', 'failed', 'blocked', 'cancelled', 'waiting'):
            break
        require(time.monotonic() - start < 120, 'Live test timed out; inspect original Turn, do not resubmit')
        sleep(1)
    require(t['status'] == 'completed', 'Live acceptance did not complete; inspect original Turn')
    stream = call(p + '/events', raw=True)
    events = [json.loads(line[5:].strip()) for line in stream.splitlines() if line.startswith('data:')]
    require(any(e.get('kind') == 'tool_completed' and e.get('tool') == args.expected_tool for e in events),
            'No successful expected tool event; text-only output is not passing evidence')
    if args.kind == 'ziwei':
        require(not args.clarify or clarified, 'Model guessed missing input instead of asking')
        require(all(e.get('tool') in (args.expected_tool, 'read_artifact') for e in events
                    if e.get('kind') in ('tool_started', 'tool_completed', 'tool_failed')),
                'Unexpected tool selected')
        evidence.update(verify_ziwei(call, t['result'], args.expected_tool))
        evidence['clarification_resumed'] = clarified
    evidence.update(validated_at=datetime.now(timezone.utc).isoformat(),
                    elapsed_seconds=time.monotonic() - start, status='passed',
                    scope=('Native tool selection, optional clarification, chart_only JSON, artifact scalar '
                           'citations and interval coverage; not interpretation quality or provider identity attestation'
                           if args.kind == 'ziwei' else 'Tool event and completed Turn; not answer quality or Feishu delivery'))
    destination.write_text(json.dumps(evidence, indent=2) + '\n')
    print(f'Live {args.kind} acceptance passed; redacted evidence: {destination}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kind', choices=['model', 'mcp', 'ziwei'], required=True)
    parser.add_argument('--prompt')
    parser.add_argument('--expected-tool', required=True)
    parser.add_argument('--clarify', action='store_true')
    args = parser.parse_args()
    require(os.environ.get('FINANCECLAW_LIVE_ACCEPTANCE') == 'true',
            'Set FINANCECLAW_LIVE_ACCEPTANCE=true only for the intended test environment')
    base = os.environ.get('FINANCECLAW_LIVE_BASE_URL', '').rstrip('/')
    token = os.environ.get('FINANCECLAW_LIVE_API_TOKEN', '')
    uri = urlsplit(base)
    require(token and uri.hostname and not (uri.username or uri.password or uri.query or uri.fragment)
            and uri.scheme in ('http', 'https') and (uri.scheme == 'https' or uri.hostname in ('127.0.0.1', 'localhost')),
            'An HTTPS test endpoint (or loopback) and API token are required')
    allowed = {'model': ['search_catalog'], 'mcp': ['searchHotels', 'getHotelDetail', 'getHotelSearchTags'], 'ziwei': ZIWEI}
    require(args.expected_tool in allowed[args.kind], 'Select an allowed read-only tool for this acceptance kind')
    require(args.kind == 'ziwei' or (args.prompt and not args.clarify), 'A non-sensitive prompt is required; clarification is Ziwei-only')
    require(args.kind != 'ziwei' or not args.prompt, 'Ziwei acceptance uses the fixed synthetic prompt')
    opener = urllib.request.build_opener(NoRedirect())

    def call(path, body=None, method=None, raw=False):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers={'Authorization': 'Bearer ' + token,
                                         'Idempotency-Key': str(uuid.uuid4()), 'Content-Type': 'application/json'})
        with opener.open(req, timeout=40) as response:
            data = response.read(2 * 1024 * 1024 + 1)
            require(len(data) <= 2 * 1024 * 1024, 'Acceptance response exceeded limit')
            return data.decode() if raw else json.loads(data)
    run(args, call)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError) as error:
        # Only locally defined validation errors are safe to display; remote bodies stay private.
        message = str(error) if isinstance(error, AcceptanceFailure) else type(error).__name__
        raise SystemExit('Acceptance failed: ' + message + '; inspect the original Turn in the test environment.')
