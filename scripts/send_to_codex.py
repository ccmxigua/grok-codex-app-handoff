#!/usr/bin/env python3
"""Send one prompt through local Codex app-server and optionally open its thread."""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import quote


class AppServerError(RuntimeError):
    pass


class AppServer:
    def __init__(self, cwd: str | None, timeout: float) -> None:
        codex = shutil.which("codex")
        if codex is None:
            raise AppServerError("codex CLI is not on PATH")
        self.timeout = timeout
        self.process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            cwd=cwd,
            bufsize=0,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.selector = selectors.DefaultSelector()
        self.fd = self.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.selector.register(self.fd, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.pending: deque[dict[str, Any]] = deque()

    def send(self, message: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise AppServerError(f"codex app-server exited with code {self.process.returncode}")
        assert self.process.stdin is not None
        wire = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        self.stdin.write(wire)
        self.stdin.flush()

    def _receive_from_server(self, deadline: float) -> dict[str, Any]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AppServerError(f"invalid JSONL from app-server: {exc}") from exc
                if not isinstance(value, dict):
                    raise AppServerError("app-server returned a non-object JSON message")
                return value

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for codex app-server")
            events = self.selector.select(remaining)
            if not events:
                raise TimeoutError("timed out waiting for codex app-server")
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                if self.buffer.strip():
                    raw = bytes(self.buffer)
                    self.buffer.clear()
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        return value
                raise AppServerError("codex app-server closed its output")
            self.buffer.extend(chunk)

    def receive(self, deadline: float) -> dict[str, Any]:
        if self.pending:
            return self.pending.popleft()
        return self._receive_from_server(deadline)

    def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            message = self._receive_from_server(deadline)
            if message.get("id") == request_id:
                if "error" in message:
                    raise AppServerError(f"{method} failed: {message['error']}")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise AppServerError(f"{method} returned no result object")
                return result
            if "id" in message and "method" in message:
                raise AppServerError(
                    "Codex requested an approval or input; nothing was auto-approved: "
                    + json.dumps(message, ensure_ascii=False)
                )
            self.pending.append(message)

    def send_notification(self, method: str, params: dict[str, Any]) -> None:
        self.send({"method": method, "params": params})

    def wait_for_turn(self, turn_id: str) -> tuple[str, str]:
        deadline = time.monotonic() + self.timeout
        messages: dict[str, tuple[str | None, str]] = {}
        while True:
            message = self.receive(deadline)
            method = message.get("method")
            params = message.get("params", {})
            if "id" in message and "method" in message:
                raise AppServerError(
                    "Codex requested an approval or input; nothing was auto-approved: "
                    + json.dumps(message, ensure_ascii=False)
                )
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    item_id = str(item.get("id", len(messages)))
                    messages[item_id] = (item.get("phase"), str(item.get("text", "")))
            if method == "turn/completed":
                turn = params.get("turn", {})
                if turn.get("id") != turn_id:
                    continue
                status = str(turn.get("status", "unknown"))
                finals = [text for phase, text in messages.values() if phase == "final_answer" and text]
                selected = finals or [text for _, text in messages.values() if text]
                answer = "\n".join(selected).strip()
                return status, answer

    def close(self) -> None:
        try:
            self.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.selector.close()
        self.stdout.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="exact user-authorized prompt")
    prompt_group.add_argument(
        "--prompt-stdin", action="store_true", help="read the exact prompt from stdin"
    )
    parser.add_argument("--thread-id", help="resume this exact local Codex thread")
    parser.add_argument("--cwd", help="optional workspace directory for the Codex thread")
    parser.add_argument("--open-app", action="store_true", help="open the same thread in Codex desktop")
    parser.add_argument("--timeout-seconds", type=float, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prompt = sys.stdin.read() if args.prompt_stdin else args.prompt
    if not prompt:
        print("prompt must not be empty", file=sys.stderr)
        return 2
    if args.timeout_seconds <= 0:
        print("timeout-seconds must be positive", file=sys.stderr)
        return 2

    client: AppServer | None = None
    thread_id: str | None = args.thread_id
    turn_start_sent = False
    turn_completed = False
    try:
        client = AppServer(args.cwd, args.timeout_seconds)
        client.request(
            1,
            "initialize",
            {"clientInfo": {"name": "grok_bot", "title": "Grok Bot", "version": "1.0"}},
        )
        client.send_notification("initialized", {})
        if thread_id:
            thread_result = client.request(2, "thread/resume", {"threadId": thread_id})
        else:
            params: dict[str, Any] = {}
            if args.cwd:
                params["cwd"] = os.path.abspath(args.cwd)
            thread_result = client.request(2, "thread/start", params)
            thread_id = thread_result.get("thread", {}).get("id")
        if not thread_id:
            raise AppServerError("Codex did not return a thread ID")

        turn_start_sent = True
        turn_result = client.request(
            3,
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
        )
        turn_id = turn_result.get("turn", {}).get("id")
        if not turn_id:
            raise AppServerError("Codex did not return a turn ID")
        status, answer = client.wait_for_turn(str(turn_id))
        if status != "completed":
            raise AppServerError(f"Codex turn ended with status {status}")
        turn_completed = True
        if not answer:
            raise AppServerError("Codex turn completed without a final agent message")

        app_open_requested = False
        app_open_error: str | None = None
        if args.open_app:
            url = f"codex://threads/{quote(str(thread_id), safe='')}"
            try:
                opened = subprocess.run(["open", url], check=False)
                app_open_requested = opened.returncode == 0
                if not app_open_requested:
                    app_open_error = f"open exited with code {opened.returncode}"
            except OSError as exc:
                app_open_error = str(exc)

        print(
            json.dumps(
                {
                    "status": "completed",
                    "thread_id": thread_id,
                    "answer": answer,
                    "app_open_requested": app_open_requested,
                    "app_open_error": app_open_error,
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (AppServerError, TimeoutError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "thread_id": thread_id,
                    "turn_start_sent": turn_start_sent,
                    "delivery_uncertain": turn_start_sent and not turn_completed,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
