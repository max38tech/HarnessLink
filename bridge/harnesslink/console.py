"""Asynchronous terminal prompts that can be cancelled with their Codex request."""
import asyncio
import json
import sys


class Console:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def line(self, prompt):
        print(prompt, end='', flush=True)
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def readable():
            if not future.done():
                future.set_result(sys.stdin.readline().strip())

        loop.add_reader(sys.stdin.fileno(), readable)
        try:
            return await future
        finally:
            loop.remove_reader(sys.stdin.fileno())

    async def approve(self, method, params):
        async with self.lock:
            print('\nCodex needs PC input:', method, flush=True)
            print(json.dumps(params, ensure_ascii=False, indent=2), flush=True)
            if method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                answer = await self.line('Allow this action once? [y/N] ')
                decision = 'accept' if answer.lower() == 'y' else 'decline'
                available = params.get('availableDecisions')
                if available and decision not in available:
                    decision = 'cancel' if 'cancel' in available else 'decline'
                return {'decision': decision}
            if method == 'item/tool/requestUserInput':
                answers = {}
                for question in params.get('questions', []):
                    print(question.get('question', ''), flush=True)
                    options = question.get('options') or []
                    for i, option in enumerate(options, 1):
                        print(f"{i}. {option['label']}: {option.get('description', '')}", flush=True)
                    answer = await self.line('Answer (text or option number): ')
                    if answer.isdigit() and 1 <= int(answer) <= len(options):
                        answer = options[int(answer) - 1]['label']
                    answers[question['id']] = {'answers': [answer] if answer else []}
                return {'answers': answers}
            if method == 'item/permissions/requestApproval':
                print('Additional permissions were not granted. Configure permissions in Codex on the PC.', flush=True)
                return {'permissions': {}}
            if method == 'mcpServer/elicitation/request':
                return {'action': 'decline', 'content': None, '_meta': None}
            raise RuntimeError('Unsupported server request: ' + method)
