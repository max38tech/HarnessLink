import argparse
import asyncio
import contextlib
import getpass
import importlib.util
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
from urllib.parse import urlparse

from .codex import CodexClient, private_write
from .console import Console
from .session import DeviceSession
from .speech import LocalSpeech

ROOT = Path(__file__).resolve().parents[2]


def codex_binary():
    mise = Path.home() / '.local/share/mise/installs/codex/latest/bin/codex'
    return str(mise) if mise.exists() else (shutil.which('codex') or 'codex')


def token_for(state):
    path = state / 'device-token'
    if not path.exists():
        private_write(path, secrets.token_hex(32) + '\n')
    token = path.read_text().strip()
    if not 32 <= len(token) <= 128 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError('Device token must contain 32–128 printable ASCII characters without spaces')
    return token


def provision(args):
    import serial
    uri = urlparse(args.bridge)
    if uri.scheme != 'ws' or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
        raise ValueError('Use ws://PC_LAN_IP:8765 with no credentials or query string')
    if uri.port is not None and not 1 <= uri.port <= 65535:
        raise ValueError('Bridge port must be 1–65535')
    if any(ord(c) <= 32 or ord(c) > 126 for c in args.bridge):
        raise ValueError('Bridge URL must contain printable ASCII without spaces')
    if len(args.bridge) > 191:
        raise ValueError('Bridge URL too long')
    ssid = input('2.4 GHz Wi-Fi SSID: ')
    password = getpass.getpass('Wi-Fi password (hidden; blank for open network): ')
    if not 1 <= len(ssid.encode()) <= 32 or len(password.encode()) > 63 or '\0' in ssid + password:
        raise ValueError('SSID must be 1–32 bytes and password at most 63 bytes')
    payload = json.dumps({'op': 'configure', 'ssid': ssid, 'password': password,
                          'uri': args.bridge, 'token': token_for(args.state)}) + '\n'
    with serial.Serial(args.port, 115200, timeout=1, write_timeout=3) as port:
        # Both USB console and UART console accept the same newline-delimited configuration.
        port.write(b'\n')
        port.write(payload.encode())
        port.flush()
        for _ in range(20):
            line = port.readline()
            if b'HARNESSLINK_CONFIGURED' in line:
                print('Configured; device is restarting. Credentials were not printed.')
                return
    raise RuntimeError('No configuration acknowledgment. Verify firmware, console port, and USB data cable.')


def download(args):
    from huggingface_hub import snapshot_download
    args.models.mkdir(parents=True, exist_ok=True)
    snapshot_download('Systran/faster-whisper-base.en', local_dir=args.models / 'base.en')
    subprocess.run([sys.executable, '-m', 'piper.download_voices', 'en_US-lessac-medium'],
                   cwd=args.models, check=True)
    print('English speech models downloaded to', args.models)


def doctor(args):
    checks = []

    def check(name, ok, detail):
        checks.append(ok)
        print(('OK   ' if ok else 'MISS ') + name + ': ' + detail)

    for module in ('websockets', 'numpy', 'faster_whisper', 'piper', 'serial'):
        check(module, importlib.util.find_spec(module) is not None, 'Python package')
    binary = args.codex
    check('Codex', bool(shutil.which(binary)), binary)
    check('Whisper model', (args.models / 'base.en/model.bin').exists(), str(args.models / 'base.en'))
    check('Piper voice', (args.models / 'en_US-lessac-medium.onnx').exists()
          and (args.models / 'en_US-lessac-medium.onnx.json').exists(), str(args.models))
    ports = sorted(Path('/dev').glob('ttyACM*')) + sorted(Path('/dev').glob('ttyUSB*'))
    check('USB serial', bool(ports), ', '.join(map(str, ports)) or 'No serial devices exposed')
    print('ESP-IDF:', shutil.which('idf.py') or 'not installed / export.sh not sourced')
    print('No network connections, credentials, or model requests were used for this check.')
    return 0 if all(checks) else 1


async def serve(args):
    from websockets.asyncio.server import serve as websocket_serve
    from websockets.exceptions import ConnectionClosed
    token = token_for(args.state)
    speech = LocalSpeech(args.models)
    diagnostic = bool(args.capture or args.tone)
    if not diagnostic:
        print('Loading local speech models...', flush=True)
        await asyncio.to_thread(speech.load)
    console = Console()
    client = CodexClient([args.codex, 'app-server', '--listen', 'stdio://'], args.state, args.cwd,
                         approval=console.approve)
    if not diagnostic:
        await client.start()
    lock = asyncio.Lock()

    async def handle(socket):
        if lock.locked():
            await socket.close(code=1013, reason='Another device is connected')
            return
        async with lock:
            session = DeviceSession(socket, token, client, speech, capture=args.capture)
            client.status = session.status
            try:
                if args.tone:
                    # Authenticate with the same handshake without invoking Codex or speech models.
                    raw = await asyncio.wait_for(socket.recv(), 10)
                    import hmac
                    from .session import parse_control
                    hello = parse_control(raw) if isinstance(raw, str) else {}
                    value = hello.get('token', '')
                    if (hello.get('type') != 'hello' or hello.get('version') != 1
                            or not isinstance(value, str) or not hmac.compare_digest(value.encode(), token.encode())):
                        await socket.close(code=1008, reason='Authentication failed')
                        return
                    await session.send('ready', version=1, sample_rate=16000)
                    print('Sending one quiet 440 Hz test tone.', flush=True)
                    await play_test_tone(session)
                    await socket.close()
                    return
                await session.run()
            except ConnectionClosed:
                print('Device disconnected; partial audio discarded. No command was resubmitted.', flush=True)
            except Exception as exc:
                print(f'Device session ended: {exc}', flush=True)
                with contextlib.suppress(Exception):
                    await socket.close(code=1011, reason='Inspect PC terminal')
            finally:
                client.status = None

    try:
        async with websocket_serve(handle, args.host, args.listen_port, compression=None,
                                   max_size=65536, max_queue=4, ping_interval=20, ping_timeout=20):
            print(f'HarnessLink listening on {args.host}:{args.listen_port}; Ctrl-C stops it.', flush=True)
            print('Keep this terminal open for Codex approvals and questions.', flush=True)
            await asyncio.Future()
    finally:
        await client.close()


async def play_test_tone(session):
    from .session import parse_control

    async def receive_acks():
        while True:
            raw = await session.socket.recv()
            if isinstance(raw, str):
                msg = parse_control(raw)
                if msg['type'] in ('play_ack', 'play_done'):
                    await session.control(msg)

    pcm = b''.join(struct.pack('<h', round(700 * math.sin(2 * math.pi * 440 * i / 16000)))
                   for i in range(16000))
    reader = asyncio.create_task(receive_acks())
    playback = asyncio.create_task(session.play(pcm))
    try:
        done, _ = await asyncio.wait([reader, playback], timeout=20, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError('Speaker test timed out')
        # Surface transport errors immediately instead of waiting for the overall timeout.
        for task in done:
            task.result()
    finally:
        reader.cancel()
        playback.cancel()
        await asyncio.gather(reader, playback, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description='HarnessLink local voice bridge')
    parser.add_argument('--state', type=Path, default=ROOT / '.state')
    parser.add_argument('--models', type=Path, default=ROOT / 'models')
    parser.add_argument('--codex', default=codex_binary())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='Generate a private device token')
    sub.add_parser('doctor', help='Report dependencies, models, and USB access')
    sub.add_parser('download-models', help='Download local English STT and TTS models')
    p = sub.add_parser('provision', help='Send Wi-Fi and bridge settings over USB')
    p.add_argument('--port', required=True)
    p.add_argument('--bridge', required=True)
    s = sub.add_parser('serve', help='Run the authenticated Wi-Fi audio bridge')
    s.add_argument('--host', default='0.0.0.0')
    s.add_argument('--listen-port', type=int, default=8765)
    s.add_argument('--cwd', type=Path, default=ROOT)
    diagnostics = s.add_mutually_exclusive_group()
    diagnostics.add_argument('--capture', type=Path, help='Save a microphone WAV; bypass speech and Codex')
    diagnostics.add_argument('--tone', action='store_true', help='Play one quiet tone; bypass speech and Codex')
    args = parser.parse_args()
    args.state = args.state.resolve()
    args.models = args.models.resolve()
    os.umask(0o077)
    try:
        if args.command == 'init':
            token_for(args.state)
            print('Private device token ready in', args.state)
        elif args.command == 'doctor':
            return doctor(args)
        elif args.command == 'download-models':
            download(args)
        elif args.command == 'provision':
            provision(args)
        elif args.command == 'serve':
            args.cwd = args.cwd.resolve(strict=True)
            if not args.cwd.is_dir():
                raise ValueError('--cwd must be a directory')
            asyncio.run(serve(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'HarnessLink: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
