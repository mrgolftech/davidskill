#!/usr/bin/env python3
"""
codex-quota.py — Codex 剩余额度查询 (Python 版)

通过 codex app-server JSON-RPC 获取额度，输出 JSON 供 Hermes agent 解析展示。

Usage: python3 /path/to/codex-quota.py
Output: single JSON object to stdout
"""

import json
import sys
import subprocess
import signal
import time
import select
import os
import fcntl


CODEX = "/root/.hermes/node/bin/codex"
TIMEOUT_OVERALL = 12  # 整体超时秒数
TIMEOUT_PER_REQUEST = 8  # 单请求超时秒数


def clamp_percent(v):
    """安全 clamp 百分比到 0-100，保留一位小数"""
    if not isinstance(v, (int, float)):
        return None
    return max(0.0, min(100.0, round(v, 1)))


def remaining_percent(bucket):
    """从 bucket 计算剩余百分比"""
    if not bucket or "usedPercent" not in bucket:
        return None
    used = bucket["usedPercent"]
    if not isinstance(used, (int, float)):
        return None
    return clamp_percent(100.0 - used)


def bucket_name(mins):
    """根据窗口分钟数返回可读名称"""
    if mins == 300:
        return "5h"
    if mins == 10080:
        return "weekly"
    if isinstance(mins, (int, float)):
        return f"{mins}min"
    return "unknown"


class JSONRPCClient:
    """
    最小化 JSON-RPC 客户端，通过 stdio 与 codex app-server 通信。

    每行是一个完整的 JSON 消息。请求带 id，响应带相同 id。
    通知（notification）不带 id，不期待响应。
    """

    def __init__(self, binary):
        self.binary = binary
        self.proc = None
        self._next_id = 1
        self._stderr_lines = []
        self._stdout_buf = ""

    def start(self):
        """启动 app-server 子进程"""
        self.proc = subprocess.Popen(
            [self.binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # 设 stdout 为非阻塞
        fd = self.proc.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def stop(self):
        """终止子进程"""
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _read_message(self, timeout):
        """
        从 stdout 读取一条完整的 JSON 消息（一行）。

        返回解析后的 dict。如果读到无 id 的通知则跳过，继续读下一条。
        超时或 EOF 抛异常。
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(0.001, deadline - time.time())

            # 先检查缓冲区里是否已经有完整行
            if "\n" in self._stdout_buf:
                line, self._stdout_buf = self._stdout_buf.split("\n", 1)
                if line.strip():
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        # 非 JSON 行，跳过
                        continue

            # 从非阻塞 stdout 读数据
            try:
                ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
                if ready:
                    chunk = self.proc.stdout.read(4096)
                    if chunk == "":
                        raise EOFError("app-server stdout closed")
                    self._stdout_buf += chunk
                # select 超时，继续循环
            except (OSError, BlockingIOError):
                # 非阻塞读暂时没数据
                time.sleep(0.05)
                continue

            # 收集 stderr
            self._collect_stderr()

        raise TimeoutError(f"read timeout after {timeout}s")

    def _collect_stderr(self):
        """非阻塞收集 stderr（用于排查）"""
        try:
            fd = self.proc.stderr.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break
                self._stderr_lines.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    def request(self, method, params=None):
        """发送 JSON-RPC 请求，等待匹配 id 的响应"""
        req_id = self._next_id
        self._next_id += 1

        payload = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        req_line = json.dumps(payload, ensure_ascii=False)
        try:
            self.proc.stdin.write(req_line + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError(f"app-server stdin closed (method={method})")

        # 循环读消息，跳过无 id 的通知，直到收到匹配的响应
        msg_start = time.time()
        while time.time() - msg_start < TIMEOUT_PER_REQUEST:
            remaining = TIMEOUT_PER_REQUEST - (time.time() - msg_start)
            msg = self._read_message(remaining)

            # 无 id → 服务器通知，跳过
            if "id" not in msg or msg["id"] is None:
                continue

            if msg["id"] != req_id:
                raise RuntimeError(
                    f"response id mismatch: expected {req_id}, got {msg.get('id')}"
                )

            if "error" in msg:
                raise RuntimeError(f"{method} error: {json.dumps(msg['error'])}")

            return msg.get("result")

        raise TimeoutError(f"{method} timeout after {TIMEOUT_PER_REQUEST}s")

    def notify(self, method, params=None):
        """发送 JSON-RPC 通知（无 id，不等待响应）"""
        payload = {"method": method}
        if params is not None:
            payload["params"] = params

        try:
            self.proc.stdin.write(
                json.dumps(payload, ensure_ascii=False) + "\n"
            )
            self.proc.stdin.flush()
        except BrokenPipeError:
            raise RuntimeError(
                f"app-server stdin closed (notification: {method})"
            )


def main():
    client = JSONRPCClient(CODEX)

    try:
        client.start()

        # Step 1: initialize
        client.request("initialize", {
            "clientInfo": {
                "name": "codex_quota_check",
                "title": "Codex Quota Check",
                "version": "0.2.0",
            },
        })

        # Step 2: initialized (通知，不期待响应)
        client.notify("initialized")

        # Step 3: 读限额
        result = client.request("account/rateLimits/read")

        # 兼容新旧两种返回格式
        snapshot = None
        if isinstance(result, dict):
            snapshot = (
                result.get("rateLimitsByLimitId", {}).get("codex")
                or result.get("rateLimits")
            )

        output = {
            "planType": None,
            "rateLimitReachedType": None,
            "credits": None,
            "buckets": [],
        }

        if snapshot and isinstance(snapshot, dict):
            for key in ("primary", "secondary"):
                bucket = snapshot.get(key)
                if not bucket or not isinstance(bucket, dict):
                    continue
                mins = bucket.get("windowDurationMins")
                used = bucket.get("usedPercent")
                resets_ts = bucket.get("resetsAt")
                output["buckets"].append({
                    "name": bucket_name(mins),
                    "key": key,
                    "usedPercent": used,
                    "remainingPercent": remaining_percent(bucket),
                    "windowDurationMins": mins,
                    "resetsAt": resets_ts,
                    "resetsAtISO": (
                        time.strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z",
                            time.gmtime(resets_ts),
                        )
                        if isinstance(resets_ts, (int, float))
                        else None
                    ),
                })

            output["rateLimitReachedType"] = snapshot.get(
                "rateLimitReachedType"
            )

        output["planType"] = (
            snapshot.get("planType") if snapshot else None
        ) or result.get("planType")
        output["credits"] = (
            snapshot.get("credits") if snapshot else None
        ) or result.get("credits")

        print(json.dumps(output, ensure_ascii=False))

    except Exception as e:
        stderr_info = ""
        if client._stderr_lines:
            stderr_info = " | stderr: " + " | ".join(
                client._stderr_lines[-3:]
            )
        print(
            json.dumps({"error": f"{e}{stderr_info}"}, ensure_ascii=False),
            file=sys.stderr,
        )
        sys.exit(1)

    finally:
        client.stop()


if __name__ == "__main__":
    # 整体超时：用 SIGALRM
    signal.signal(signal.SIGALRM, lambda sig, frame: sys.exit(1))
    signal.alarm(TIMEOUT_OVERALL)
    main()
