# -*- coding: utf-8 -*-
"""PubMed 检索结果缓存。

为什么需要这个模块
------------------
1. **慢**：一次 esearch 要走一次 HTTPS 往返，NCBI 还限速（无 key 时 3 请求/秒）。
   同一个课题反复调参（改检索式、加日期过滤）时，同一条检索式会被打很多次。
2. **贵**：模型侧的检索式生成每次都要 LLM 调用，检索式往往大同小异；
   如果检索结果能复用，就能把省下的时间/额度投到真正的新问题上。
3. **不稳**：NCBI 偶发返回 HTTP 200 + esearchresult.ERROR（见 pubmed.py 的踩坑记录），
   这类故障结果**绝不能**进缓存，否则一次抖动会被放大成"整个课题检索不到"。

设计约束
--------
- 缓存目录放在 `paths.runs_root` 的**同级** `.medlit-cache/`，不放在 runs 目录里——
  runs 目录是每次运行的产物目录，混进缓存文件会污染交付物。
- 只缓存成功结果（无 error 且 count > 0）。故障结果和 0 命中都不缓存。
- TTL 默认 72 小时：PubMed 的收录变化以"天"为单位，两三天内复用检索结果
  对证据检索是可接受的；再久则应该重新检索。
- 写入必须**原子**（临时文件 + os.replace），否则并发进程/线程写到一半被读到，
  会得到半截 JSON（get 必须静默当成未命中，而不是抛异常打断主流程）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time

# 缓存目录名（放在 runs_root 同级，不污染运行产物目录）
CACHE_DIR_NAME = '.medlit-cache'

# 默认 TTL（小时）。PubMed 收录变化以天计，两三天内复用对证据检索可接受。
DEFAULT_TTL_HOURS = 72.0

# 缓存文件的键名前缀兼版本号：格式变更时改这里，旧缓存自动失效。
_KEY_VERSION = 'v1'

_WS = re.compile(r'\s+')

# 缓存键允许的字符集。键会直接拼成文件名，所以必须严格限制：
# 否则 `../../evil` 这类键会写到缓存目录之外去。
_SAFE_KEY = re.compile(r'^[A-Za-z0-9_-]{1,64}$')


# ---------------- 参数规范化 ----------------
def _norm_text(value) -> str:
    """规范化自由文本：去首尾空白 + 连续空白压成一个空格 + 转小写。

    PubMed 检索本身不区分大小写，`"Diabetes  Mellitus"` 与 `"diabetes mellitus"`
    检索结果相同，若不规范化就会产生两份缓存、白白多打一次 NCBI。
    """
    if value is None:
        return ''
    return _WS.sub(' ', str(value).strip()).lower()


def _norm_mindate(mindate) -> str:
    """规范化日期下界。

    `None` 与 `''` 必须归一化成同一个值——两者在 pubmed.esearch 里语义完全一致
    （`if mindate:` 为假时不加 datetype 过滤），分开算 key 等于制造重复缓存。
    """
    if not mindate:
        return ''
    return _WS.sub('', str(mindate).strip()).lower()


def cache_key(term: str, retmax: int = 20, sort: str = 'relevance',
              mindate: str | None = None) -> str:
    """由检索参数算出缓存键（sha256 前 16 位十六进制）。

    参与 key 的每一个参数都会改变检索结果集，所以必须全部进 key：
    term / retmax / sort / mindate。少了任何一个都会命中错误的旧结果。
    """
    try:
        n_retmax = int(retmax)
    except (TypeError, ValueError):
        n_retmax = 0
    # 用 JSON 数组而不是拼接字符串：拼接时字段里含分隔符会造成键碰撞
    # （term="a|20" 与 term="a" + retmax=20 会拼出同样的串）。
    raw = json.dumps([_KEY_VERSION, _norm_text(term), n_retmax,
                      _norm_text(sort), _norm_mindate(mindate)],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def default_cache_dir(runs_root: str = './runs') -> str:
    """给出缓存目录的默认位置：`<runs_root 的父目录>/.medlit-cache`。

    刻意放在 runs_root 的**同级**而不是其内部：runs/ 下面是每次运行的交付产物，
    缓存混进去会让产物目录看起来很脏（也可能被误当成检索证据交付给用户）。
    """
    root = os.path.abspath(runs_root or './runs')
    parent = os.path.dirname(root) or os.getcwd()
    return os.path.join(parent, CACHE_DIR_NAME)


def is_cacheable(payload) -> bool:
    """判断一个 esearch 结果该不该进缓存：error 为空**且** count > 0。

    count == 0 也不缓存：检索式刚改坏时的 0 命中多半是临时状态，
    缓存下来会让"检索式写错了"这个事实被缓存住，返工时很难察觉。
    """
    if not isinstance(payload, dict):
        return False
    if str(payload.get('error') or '').strip():
        return False
    try:
        count = int(payload.get('count') or 0)
    except (TypeError, ValueError):
        return False
    return count > 0


# ---------------- 缓存读写 ----------------
class EsearchCache:
    """基于本地 JSON 文件的 esearch 结果缓存。

    每个键一个文件 `{key}.json`，内容形如：
        {'saved_at': 1730000000.0, 'payload': {'count','ids','translation','error'}}
    """

    def __init__(self, cache_dir: str, ttl_hours: float = DEFAULT_TTL_HOURS):
        # 目录由调用方传入，模块内不写死任何路径。
        self.cache_dir = os.path.abspath(os.path.expanduser(str(cache_dir)))
        self.ttl_hours = float(ttl_hours)
        # 同一进程内多个线程可能同时写同一个键，用锁避免临时文件互相覆盖。
        self._lock = threading.Lock()

    # ---- 内部工具 ----
    def _path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f'{key}.json')

    @staticmethod
    def _safe_key(key) -> bool:
        """键会拼进文件名，必须限制字符集，杜绝 `../` 之类的路径穿越。"""
        return bool(_SAFE_KEY.match(str(key or '')))

    @staticmethod
    def _normalize(payload: dict) -> dict:
        """把 payload 收敛成固定的四个字段，保证下游拿到结构一致的结果。"""
        ids = payload.get('ids') or []
        if not isinstance(ids, (list, tuple)):
            ids = []
        try:
            count = int(payload.get('count') or 0)
        except (TypeError, ValueError):
            count = 0
        return {
            'count': count,
            'ids': [str(x) for x in ids],
            'translation': str(payload.get('translation') or ''),
            'error': str(payload.get('error') or ''),
        }

    def _is_fresh(self, saved_at: float) -> bool:
        """按 TTL 判断是否过期。

        `saved_at` 缺失或解析失败一律视为过期（宁可多打一次 NCBI，
        也不要拿一份来源不明的数据当证据）。
        """
        ttl_seconds = self.ttl_hours * 3600.0
        if ttl_seconds <= 0:
            return False
        try:
            ts = float(saved_at)
        except (TypeError, ValueError):
            return False
        age = time.time() - ts
        # age < 0：时间戳在未来，说明文件被改坏或系统时钟跳变，同样当过期处理。
        if age < 0 or age >= ttl_seconds:
            return False
        return True

    # ---- 公开 API ----
    def get(self, key: str) -> dict | None:
        """取缓存；未命中 / 过期 / 文件损坏一律返回 None。

        **绝不抛异常**：缓存只是加速手段，缓存坏掉绝不能拖垮整个检索流程。
        """
        if not self._safe_key(key):
            return None
        path = self._path(key)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            # 文件不存在、权限不足、JSON 写坏了——都当成没命中。
            return None
        if not isinstance(data, dict):
            return None
        if not self._is_fresh(data.get('saved_at')):
            return None
        payload = data.get('payload')
        if not isinstance(payload, dict):
            return None
        try:
            return self._normalize(payload)
        except Exception:  # noqa: BLE001 —— 任何异常都只当未命中
            return None

    def set(self, key: str, payload: dict) -> None:
        """写入缓存（只写成功结果），原子落盘。

        原子性：先写同目录下的临时文件，fsync 后用 os.replace 替换目标文件。
        os.replace 在同一文件系统内是原子的，读者要么看到旧文件、要么看到完整新文件，
        不会读到写了一半的 JSON。
        """
        if not is_cacheable(payload):
            return
        if not self._safe_key(key):
            return
        record = {'saved_at': time.time(), 'payload': self._normalize(payload)}
        try:
            body = json.dumps(record, ensure_ascii=False, indent=1)
        except (TypeError, ValueError):
            return
        with self._lock:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=self.cache_dir, prefix=f'.{key}.',
                                           suffix='.tmp')
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(body)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self._path(key))
                except Exception:  # noqa: BLE001
                    # 写失败就清掉临时文件，避免缓存目录里堆垃圾。
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except (OSError, ValueError):
                # 缓存写不进去（磁盘满、只读目录）不能影响主流程。
                return

    def clear(self) -> int:
        """清空缓存，返回删除的文件数。

        只删 `.json` 缓存文件；残留的 `.tmp` 临时文件顺手清掉但**不计入返回值**，
        否则调用方拿到的数字会和实际缓存条目数对不上。
        """
        removed = 0
        if not os.path.isdir(self.cache_dir):
            return 0
        with self._lock:
            try:
                names = os.listdir(self.cache_dir)
            except OSError:
                return 0
            for name in names:
                path = os.path.join(self.cache_dir, name)
                if name.endswith('.tmp'):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    continue
                if not name.endswith('.json'):
                    continue
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
        return removed


class CacheStats:
    """缓存命中统计，供 trace 记录（写进产物里方便复盘"这次省了几次请求"）。"""

    def __init__(self):
        self._hit = 0
        self._miss = 0
        self._write = 0

    def hit(self) -> None:
        self._hit += 1

    def miss(self) -> None:
        self._miss += 1

    def record_write(self) -> None:
        self._write += 1

    def as_dict(self) -> dict:
        return {'hit': self._hit, 'miss': self._miss, 'write': self._write}


# ---------------- 高层接入 ----------------
def cached_esearch(pubmed, cache, term: str, retmax: int = 20,
                   sort: str = 'relevance', mindate: str | None = None,
                   stats: CacheStats | None = None) -> dict:
    """带缓存的 esearch：先查缓存，未命中才真的打 NCBI。

    返回值与 `pubmed.esearch` 完全一致（{'count','ids','translation','error'}），
    所以调用方可以把原来 `pm.esearch(...)` 直接换成 `cached_esearch(pm, cache, ...)`。

    `pubmed.esearch` 抛出的异常**原样抛出**（不吞、不包装）：上层 pipeline 靠捕获
    异常来记录 `search_failed`，这里吞掉会让故障在 trace 里消失。
    """
    key = cache_key(term, retmax=retmax, sort=sort, mindate=mindate)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            if stats is not None:
                stats.hit()
            return hit
    if stats is not None:
        stats.miss()

    result = pubmed.esearch(term, retmax=retmax, sort=sort, mindate=mindate)

    if cache is not None and isinstance(result, dict) and is_cacheable(result):
        cache.set(key, result)
        if stats is not None:
            stats.record_write()
    return result
