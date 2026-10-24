"""A subprocess fixture for exercising the real stdio RPC transport."""
import json
import sys
import subprocess


def emit(obj):
    print(json.dumps(obj), flush=True)


thread = 'thread-test'
interrupt_error = False
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get('method')
    if method == 'initialize':
        emit({'id': msg['id'], 'result': {'userAgent': 'test'}})
    elif method in ('thread/start', 'thread/resume'):
        emit({'id': msg['id'], 'result': {'thread': {'id': thread, 'status': {'type': 'idle'}}}})
    elif method == 'turn/start':
        text = msg['params']['input'][0]['text']
        interrupt_error = text == 'wait-interrupt-error'
        emit({'method': 'turn/started', 'params': {'threadId': thread, 'turn': {'id': 'turn-test', 'status': 'inProgress'}}})
        emit({'id': msg['id'], 'result': {'turn': {'id': 'turn-test', 'status': 'inProgress'}}})
        if text == 'crash':
            sys.exit(2)
        if text in ('wait', 'wait-interrupt-error'):
            continue
        emit({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': 'turn-test', 'item': {'id': 'comment', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'Working...'}}})
        if text == 'approval':
            emit({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': thread, 'turnId': 'turn-test', 'itemId': 'exec-1', 'command': 'touch example', 'availableDecisions': ['accept', 'decline']}})
            continue
        reply = 'Hello back.'
        if text == 'spawn-child':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            reply = str(child.pid)
        emit({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': 'turn-test', 'item': {'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer', 'text': reply}}})
        status = 'failed' if text == 'fail' else 'completed'
        emit({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': 'turn-test', 'status': status, 'error': {'message': 'test failure'} if status == 'failed' else None}}})
        if text == 'spawn-child':
            sys.exit(0)
    elif method == 'turn/interrupt':
        if interrupt_error:
            emit({'id': msg['id'], 'error': {'code': -32000, 'message': 'interrupt refused'}})
            continue
        emit({'id': msg['id'], 'result': {}})
        emit({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': 'turn-test', 'status': 'interrupted'}}})
    elif msg.get('id') == 'approval-1' and 'result' in msg:
        decision = msg['result']['decision']
        emit({'method': 'item/completed', 'params': {'threadId': thread, 'turnId': 'turn-test', 'item': {'id': 'final', 'type': 'agentMessage', 'phase': 'final_answer', 'text': 'Decision: ' + decision}}})
        emit({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': 'turn-test', 'status': 'completed'}}})
