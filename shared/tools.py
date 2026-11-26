import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


class ToolError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class ToolClient:
    def __init__(self, base_url, run_id, token):
        self.url, self.token = f'{base_url}/internal/runs/{run_id}/tools', token

    def call(self, name, **arguments):
        request = Request(self.url, json.dumps({'name': name, 'arguments': arguments}).encode(),
                          {'Content-Type': 'application/json', 'X-Repair-Token': self.token}, method='POST')
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            try:
                detail = json.load(error)['error']
                raise ToolError(detail['code'], detail['message']) from error
            except (ValueError, KeyError):
                raise ToolError('tool_http_error', f'Tool returned HTTP {error.code}') from error
