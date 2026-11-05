# -*- coding: utf-8 -*-
"""模型服务端点客户端 + 按角色降级路由。零第三方依赖。

设计要点
--------
1. **provider 抽象**：任何 OpenAI 兼容端点都能接（硅基流动 / OpenAI / DeepSeek /
   Kimi / 通义 / 本地 Ollama / vLLM）。端点地址与密钥全部来自 config，不写死在代码里。
2. **rerank 是可选增强**：OpenAI 官方没有 /rerank 端点。配置里关掉或端点不支持时，
   上层自动退回"原始顺序"，功能不受影响，只是排序变粗。
3. **按角色降级**：每个角色一条候选链；调用失败自动顺延到下一个，并把失败者拉黑，
   后续不再重试。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

UA = 'medlit-agent/2.0 (evidence retrieval; https://github.com/)'


# ---------------- HTTP ----------------
def _post(url: str, payload: dict, headers: dict, timeout: int = 180,
          retries: int = 3) -> dict:
    """带 429/5xx 指数退避重试的 POST。"""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        req.add_header('User-Agent', UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            detail = ''
            try:
                detail = e.read().decode('utf-8', 'ignore')[:300]
            except Exception:
                pass
            last = RuntimeError(f'HTTP {e.code}: {detail}')
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(2 ** attempt * 2)
                continue
            raise last
        except (socket.timeout, TimeoutError) as e:
            # 超时**不重试**：同样的请求再发几次大概率还是超时，
            # 而每次重试都要等满一个 timeout，会把耗时成倍放大。
            # 直接抛给上层，由降级链换模型（那才是有效的应对）。
            raise RuntimeError(f'请求超时（{timeout}s 未响应）: {e}') from e
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


def _get(url: str, headers: dict, timeout: int = 60, retries: int = 3) -> dict:
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', UA)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            last = RuntimeError(f'HTTP {e.code}')
            if e.code == 429 or 500 <= e.code < 600:
                time.sleep(2 ** attempt * 2)
                continue
            raise last
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise last  # type: ignore[misc]


# ---------------- Provider ----------------
class Provider:
    """一个 OpenAI 兼容的模型服务端点。"""

    def __init__(self, name: str, base_url: str, api_key: str,
                 timeout: int = 180, retries: int = 3):
        self.name = name or 'provider'
        self.base = (base_url or '').rstrip('/')
        self.key = api_key or ''
        self.timeout = timeout
        self.retries = retries
        self._live: set[str] | None = None

    # ---- 基础 ----
    def _headers(self) -> dict:
        return {'Authorization': f'Bearer {self.key}'} if self.key else {}

    def available_models(self, refresh: bool = False) -> set[str]:
        """探测平台在售模型。失败时返回空集合（上层会退化为"全部尝试"）。"""
        if self._live is None or refresh:
            try:
                data = _get(f'{self.base}/models', self._headers(),
                            timeout=self.timeout, retries=self.retries)
                self._live = {m.get('id', '') for m in (data.get('data') or [])}
            except Exception:  # noqa: BLE001
                self._live = set()
        return self._live

    # ---- 对话 ----
    def chat(self, model: str, messages: list[dict], temperature: float = 0.2,
             max_tokens: int = 4000, timeout: int | None = None) -> dict:
        t0 = time.time()
        payload = {
            'model': model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'stream': False,
        }
        data = _post(f'{self.base}/chat/completions', payload, self._headers(),
                     timeout=timeout or self.timeout, retries=self.retries)
        choices = data.get('choices') or []
        content = (choices[0].get('message', {}).get('content') or '') if choices else ''
        usage = data.get('usage') or {}
        return {
            'content': content,
            'model': model,
            'elapsed': round(time.time() - t0, 1),
            'tokens': usage.get('total_tokens'),
        }

    # ---- 嵌入（OpenAI 兼容，普遍支持）----
    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        payload = {'model': model, 'input': texts, 'encoding_format': 'float'}
        data = _post(f'{self.base}/embeddings', payload, self._headers(),
                     timeout=120, retries=self.retries)
        return [d.get('embedding', []) for d in (data.get('data') or [])]

    # ---- 重排（可选，多数兼容端点没有）----
    def rerank(self, model: str, query: str, docs: list[str], top_n: int = 8,
               endpoint: str | None = None) -> list[dict]:
        url = endpoint or f'{self.base}/rerank'
        if not url.startswith('http'):
            url = f'{self.base}/{url.lstrip("/")}'
        payload = {'model': model, 'query': query, 'documents': docs, 'top_n': top_n}
        data = _post(url, payload, self._headers(), timeout=90,
                     retries=self.retries)
        return data.get('results') or []


# ---------------- 角色路由 ----------------
class ModelRouter:
    """按角色路由：先探活，命中第一个可用模型；失败则顺延到下一个。"""

    def __init__(self, provider: Provider, chain: dict[str, list[str]],
                 call_budget: int = 240):
        self.p = provider
        self.chain = chain or {}
        self._resolved: dict[str, str] = {}
        self._fail: dict[str, set[str]] = {r: set() for r in self.chain}
        # 一次 chat 调用的**总时长预算**（秒）。
        # 没有它的话，最坏耗时 = 降级链长度 × timeout × retries
        # （reader 链有 4 个模型，180s×3 重试 → 36 分钟一次调用），
        # 任何一个模型挂起都会把整轮检索拖死。
        self.call_budget = int(call_budget or 240)
        self.last_errors: dict[str, list[str]] = {}

    def chain_of(self, role: str) -> list[str]:
        return [m for m in self.chain.get(role, []) if m not in self._fail.get(role, set())]

    def _pick(self, role: str) -> str:
        cands = self.chain_of(role)
        if not cands:
            raise RuntimeError(f'角色 {role} 已无可用模型（全部失败）')
        live = self.p.available_models()
        for m in cands:
            if not live or m in live:
                return m
        return cands[0]

    def primary(self, role: str) -> str:
        if role not in self._resolved:
            self._resolved[role] = self._pick(role)
        return self._resolved[role]

    def chat(self, role: str, messages: list[dict], temperature: float = 0.2,
             max_tokens: int = 4000, timeout: int | None = None) -> dict:
        """调用该角色；失败自动降级到链上的下一个模型。

        降级链受**总时长预算**约束：预算耗尽就停，不再往下试。
        否则「超时 × 重试 × 降级链」会相乘——实测一次精读调用能拖到半小时，
        整轮检索看起来像卡死了。
        """
        errors = []
        deadline = time.time() + self.call_budget
        for model in self.chain_of(role):
            remaining = deadline - time.time()
            if remaining <= 5:
                errors.append('总时长预算已耗尽')
                break
            # 单次调用的超时不能超过剩余预算
            t = timeout or self.p.timeout
            if t > remaining:
                t = int(remaining)
            try:
                res = self.p.chat(model, messages, temperature, max_tokens, t)
                if res.get('content'):
                    self._resolved[role] = model
                    self.last_errors[role] = errors
                    return res
                errors.append(f'{model}: 空返回')
            except Exception as e:  # noqa: BLE001
                errors.append(f'{model}: {e}')
                self._fail.setdefault(role, set()).add(model)
        self.last_errors[role] = errors
        raise RuntimeError(f'角色 {role} 全部模型失败 → ' + ' | '.join(errors[-3:]))
