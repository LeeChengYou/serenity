"""
serenity/llm_local.py
本地 LLM 後端（Ollama / OpenAI-compatible）

契約（b-R2）：
  call_local_llm(messages, system=None, temperature=0.3,
                 model=None, base_url=None, timeout=120) -> str
  is_local_llm_up(base_url=None) -> bool
  LocalLLMUnavailable（例外類別）

HTTP 實作：用 urllib（與 serenity/gemini.py 一致，不加外部依賴）。
Prefix cache 友善：呼叫端組 prompt 時，不變內容（skill 準則）排最前，
變動內容（市場快照、個股檢索）排後（實作註記）。
"""
import json
import urllib.error
import urllib.request

from .config import get_setting


class LocalLLMUnavailable(Exception):
    """本地模型未啟動或連線失敗時拋出。"""


def _resolve(model: str | None, base_url: str | None) -> tuple[str, str]:
    """解析 model / base_url：缺省時從 settings 讀取。"""
    m = model or get_setting("local_llm_model") or "qwen3:14b"
    b = (base_url or get_setting("local_llm_base_url") or "http://127.0.0.1:11434").rstrip("/")
    return m, b


def call_local_llm(
    messages: list[dict],
    system: str | None = None,
    temperature: float = 0.3,
    model: str | None = None,
    base_url: str | None = None,
    timeout: int = 120,
) -> str:
    """
    呼叫本地 Ollama（OpenAI-compatible）`/v1/chat/completions`。

    messages: OpenAI 格式 [{"role": "user"|"assistant", "content": "..."}]
    system:   若非 None，插入 role='system' 訊息排最前（prefix cache 友善）。

    連線失敗/逾時 → raise LocalLLMUnavailable（zh-TW 訊息）。
    回應非預期結構 → raise（顯式，不靜默補值）。
    """
    m, b = _resolve(model, base_url)
    url = f"{b}/v1/chat/completions"

    # 組合 messages：system 排最前（不變內容），動態內容在後
    full_messages: list[dict] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    payload = {
        "model": m,
        "messages": full_messages,
        "temperature": temperature,
        "keep_alive": -1,  # 模型常駐 VRAM
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise LocalLLMUnavailable(
            f"本地模型未啟動：請先啟動 Ollama（ollama serve）並確認已 pull {m}（錯誤：{exc.reason}）"
        ) from exc
    except TimeoutError as exc:
        raise LocalLLMUnavailable(
            f"本地模型未啟動：請先啟動 Ollama（ollama serve）並確認已 pull {m}（逾時）"
        ) from exc

    # 解析回應（非預期結構 → 顯式 raise）
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            f"本地 LLM 回應格式非預期（缺少 choices[0].message.content）：{body!r}"
        ) from exc


def is_local_llm_up(base_url: str | None = None) -> bool:
    """
    快速健檢：GET {base}/api/tags，1 秒逾時。
    有回應（任何 2xx）→ True；其餘 → False。
    """
    _, b = _resolve(None, base_url)
    url = f"{b}/api/tags"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=1):
            return True
    except Exception:
        return False
