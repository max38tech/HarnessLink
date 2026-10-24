"""Codex app-server JSON-RPC over stdio; no terminal keystroke injection."""
import asyncio
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal

VOICE_INSTRUCTIONS = (
    'You are speaking through a small push-to-talk voice terminal. '
    'Give brief, natural English final answers, normally one to three sentences. '
    'Perform requested work normally and keep technical details in tool activity. '
    'Mention failures and required user actions clearly. Avoid code blocks in final replies '
    'unless the user explicitly asks for code. Never claim unverified work succeeded.'
)


def private_write(path: Path, text: str, append=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    with os.fdopen(os.open(path, flags, 0o600), 'w') as stream:
        stream.write(text)


class CodexClient:
    def __init__(self, command, state_dir: Path, cwd: Path, approval=None, status=None):
        self.command, self.state_dir, self.cwd = command, state_dir, cwd
        self.approval, self.status = approval, status
        self.proc = None
        self.reader = None
        self.pending = {}
        self.requests = {}
        self.counter = 0
        self.write_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()
        self.thread_id = None
        self.turn_id = None
        self.done = None
        self.started = asyncio.Event()
        self.messages = []
        self.healthy = True

    def log(self, event):
        private_write(self.state_dir / 'transcript.jsonl', json.dumps({
            'time': datetime.now(timezone.utc).isoformat(), 'event': event,
        }, ensure_ascii=False) + '\n', append=True)

    async def start(self):
        self.healthy = True
        self.done = None
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            # Keep app-server's own diagnostics visible in the PC terminal.
            stderr=None, limit=8 * 1024 * 1024,
            start_new_session=True,
        )
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request('initialize', {'clientInfo': {
                'name': 'harnesslink', 'title': 'HarnessLink', 'version': '0.1.0',
            }, 'capabilities': {'experimentalApi': False}})
            await self.send({'method': 'initialized', 'params': {}})
            path = self.state_dir / 'thread.json'
            params = {'cwd': str(self.cwd), 'developerInstructions': VOICE_INSTRUCTIONS}
            if path.exists():
                saved = json.loads(path.read_text())
                if saved['cwd'] != str(self.cwd):
                    raise RuntimeError('Saved conversation uses a different cwd; use a new --state directory')
                params.update(threadId=saved['threadId'], excludeTurns=True)
                result = await self.request('thread/resume', params)
            else:
                result = await self.request('thread/start', params)
            thread = result['thread']
            self.thread_id = thread['id']
            private_write(path, json.dumps({'threadId': self.thread_id, 'cwd': str(self.cwd)}) + '\n')
            # A prior turn may have survived a lost client. Do not steer it accidentally.
            if thread.get('status', {}).get('type') in ('active', 'systemError'):
                raise RuntimeError('Saved Codex thread is active or unhealthy; inspect it on the PC before continuing')
        except BaseException:
            await self.close()
            raise

    async def send(self, message):
        async with self.write_lock:
            if not self.proc or self.proc.returncode is not None:
                raise RuntimeError('Codex connection closed')
            self.proc.stdin.write((json.dumps(message) + '\n').encode())
            await self.proc.stdin.drain()

    async def request(self, method, params):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({'id': ident, 'method': method, 'params': params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(ident, None)

    async def _read(self):
        try:
            while line := await self.proc.stdout.readline():
                message = json.loads(line)
                self.log(message)
                if 'method' not in message:
                    future = self.pending.get(message.get('id'))
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(RuntimeError(str(message['error'])))
                        else:
                            future.set_result(message.get('result', {}))
                    continue
                method, params = message['method'], message.get('params', {})
                if 'id' in message:
                    task = asyncio.create_task(self._server_request(message))
                    self.requests[message['id']] = task
                    task.add_done_callback(lambda t, i=message['id']: self.requests.pop(i, None))
                    continue
                if method == 'serverRequest/resolved':
                    task = self.requests.get(params.get('requestId'))
                    if task:
                        task.cancel()
                if params.get('threadId') != self.thread_id:
                    continue
                if method == 'turn/started' and self.done and not self.done.done():
                    self.turn_id = params['turn']['id']
                    self.started.set()
                if method == 'item/completed' and self.done and not self.done.done():
                    item = params.get('item', {})
                    if params.get('turnId') == self.turn_id and item.get('type') == 'agentMessage':
                        self.messages.append(item)
                        # Transcript retains complete activity; speech need only retain final messages.
                        self.messages = self.messages[-32:]
                if method == 'turn/completed' and self.done and not self.done.done():
                    if params['turn']['id'] == self.turn_id:
                        self.done.set_result(params['turn'])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log({'bridgeError': type(exc).__name__})
        finally:
            self.healthy = False
            error = RuntimeError('Codex connection closed; inspect transcript before retrying')
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(error)
            if self.done and not self.done.done():
                self.done.set_exception(error)
            self.started.set()

    async def _server_request(self, message):
        ident, method, params = message['id'], message['method'], message.get('params', {})
        try:
            if self.status:
                await self.status('approval')
            if self.approval:
                result = await self.approval(method, params)
            elif method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                result = {'decision': 'decline'}
            else:
                await self.send({'id': ident, 'error': {'code': -32601, 'message': 'Unsupported client request'}})
                return
            await self.send({'id': ident, 'result': result})
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await self.send({'id': ident, 'error': {'code': -32603, 'message': 'PC approval handler failed'}})
        finally:
            if self.status:
                with contextlib.suppress(Exception):
                    await self.status('processing')

    async def ask(self, text: str) -> str:
        async with self.turn_lock:
            if not self.healthy:
                raise RuntimeError('Codex execution state is uncertain; inspect the PC thread and restart the bridge')
            self.done = asyncio.get_running_loop().create_future()
            self.started.clear()
            self.turn_id = None
            self.messages = []
            self.log({'user': text})
            start = asyncio.create_task(self.request('turn/start', {
                'threadId': self.thread_id, 'input': [{'type': 'text', 'text': text}],
            }))
            try:
                result = await asyncio.shield(start)
                self.turn_id = result['turn']['id']
                self.started.set()
                turn = await asyncio.wait_for(asyncio.shield(self.done), 1800)
                if turn['status'] != 'completed':
                    raise RuntimeError((turn.get('error') or {}).get('message') or ('Codex turn ' + turn['status']))
                finals = [x['text'] for x in self.messages if x.get('phase') == 'final_answer']
                if not finals:
                    finals = [x['text'] for x in self.messages if x.get('phase') is None][-1:]
                if not finals:
                    raise RuntimeError('Codex completed without a final spoken answer; inspect PC transcript')
                return '\n'.join(finals)
            except (asyncio.CancelledError, TimeoutError):
                # Resolve an in-flight start before interrupting: never assume cancellation undoes work.
                with contextlib.suppress(Exception):
                    result = await start
                    self.turn_id = result['turn']['id']
                    self.started.set()
                    await self.interrupt()
                raise
            finally:
                self.started.set()
                if not self.done.done():
                    self.healthy = False
                    self.log({'bridgeError': 'Turn state uncertain; further commands blocked'})
                    self.done.cancel()
                # Retrieve possible EOF errors even when turn/start failed first.
                if self.done and self.done.done() and not self.done.cancelled():
                    self.done.exception()
                self.turn_id = None

    async def interrupt(self):
        if self.done is None or self.done.done():
            return
        await asyncio.wait_for(self.started.wait(), 35)
        if self.turn_id and not self.done.done():
            await self.request('turn/interrupt', {'threadId': self.thread_id, 'turnId': self.turn_id})
            await asyncio.wait_for(asyncio.shield(self.done), 30)

    async def close(self):
        for task in list(self.requests.values()):
            task.cancel()
        await asyncio.gather(*list(self.requests.values()), return_exceptions=True)
        if self.proc:
            # A tool/helper may inherit stdout after app-server exits. Own the whole group
            # so shutdown cannot hang indefinitely on that inherited pipe.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                await asyncio.wait_for(self.proc.wait(), 2)
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.proc = None
