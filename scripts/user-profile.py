#!/usr/bin/env python3
"""Download/edit/upload profile.md with an owner-bound version; never merges or auto-overwrites."""
import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 8192
VERSION = re.compile(r'"profile-[1-9][0-9]*-[0-9]+-[a-f0-9]{64}"')


class Invalid(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Invalid(code)


def base_url(value):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in ('http', 'https') and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query
            and not parsed.fragment and parsed.path in ('', '/'), 'invalid_url')
    require(parsed.scheme == 'https' or parsed.hostname in ('localhost', '127.0.0.1', '::1'),
            'https_required')
    return value.rstrip('/')


def markdown(raw):
    require(len(raw) <= MAX_BYTES, 'profile_too_large')
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        raise Invalid('invalid_profile_markdown') from None
    require('\x00' not in text, 'invalid_profile_markdown')
    return raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Invalid('redirect_refused')


def exchange(url, token, raw=None, version=None):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'text/markdown'}
    if raw is not None:
        headers.update({'Content-Type': 'text/markdown; charset=UTF-8', 'If-Match': version})
    request = urllib.request.Request(url + '/v1/memory/profile.md', data=raw,
                                     headers=headers, method='GET' if raw is None else 'PUT')
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            require(response.status == 200, 'invalid_response')
            require(response.headers.get_content_type() == 'text/markdown', 'invalid_response')
            current = response.headers.get('ETag', '')
            require(VERSION.fullmatch(current), 'invalid_response')
            body = markdown(response.read(MAX_BYTES + 1))
            return body, current
    except urllib.error.HTTPError as error:
        # Server bodies may contain sensitive text; display only the HTTP status.
        raise Invalid('http_' + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise Invalid('connection_failed') from None


def read_file(path, limit):
    require(not path.is_symlink(), 'symlink_refused')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, 'file_too_large')
    return raw


def write_file(path, raw):
    require(not path.is_symlink(), 'symlink_refused')
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '-',
                                         delete=False) as temporary:
            name = temporary.name
            os.chmod(name, 0o600)
            temporary.write(raw)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def sync(action, url, token, path, force=False):
    url = base_url(url)
    require(token and not any(c.isspace() for c in token), 'missing_or_invalid_token')
    metadata = path.with_name('.' + path.name + '.version.json')
    require(not path.is_symlink() and not metadata.is_symlink(), 'symlink_refused')
    if action == 'pull':
        require(force or not path.exists() and not metadata.exists(), 'local_file_exists')
        raw, version = exchange(url, token)
        # A partial local write never advances an upload: remove the old version first.
        if metadata.exists():
            metadata.unlink()
        write_file(path, raw)
    else:
        require(path.is_file() and metadata.is_file(), 'pull_required')
        saved = json.loads(read_file(metadata, 4096))
        require(isinstance(saved, dict) and saved.get('url') == url
                and isinstance(saved.get('etag'), str)
                and VERSION.fullmatch(saved['etag']), 'invalid_local_version')
        raw = markdown(read_file(path, MAX_BYTES))
        returned, version = exchange(url, token, raw, saved['etag'])
        require(returned == raw, 'unexpected_server_document')
    write_file(metadata, (json.dumps({'url': url, 'etag': version}) + '\n').encode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('pull', 'push'))
    parser.add_argument('--url', default='http://localhost:8080')
    parser.add_argument('--file', type=Path, default=Path('profile.md'))
    parser.add_argument('--force', action='store_true', help='pull only: replace local files')
    args = parser.parse_args()
    try:
        require(not args.force or args.action == 'pull', 'force_is_pull_only')
        sync(args.action, args.url, os.environ.get('FINANCECLAW_API_TOKEN', ''), args.file, args.force)
    except (Invalid, OSError, ValueError) as error:
        print('Profile sync failed: ' + (str(error) if isinstance(error, Invalid) else 'invalid_local_file'))
        return 1
    print('Profile downloaded.' if args.action == 'pull' else 'Profile saved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
