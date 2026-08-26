from logging import log

import requests
import json
import re
import threading
import urllib3
import openai
import httpx
from pathlib import Path
from textwrap import dedent
from utils import helpers
from anthropic import Anthropic
from services.log_chatbot_service import load_skills_from_data_dir, get_builtin_skills
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# Adapter: wraps an Anthropic client with an OpenAI-compatible interface so
# all existing  client.chat.completions.create(...)  call-sites work unchanged.
# Handles: tool use, multi-turn history with tool results, temperature/top_p
# constraints, and the OpenAI ↔ Anthropic message format differences.
# ---------------------------------------------------------------------------

class _AnthropicFunctionAdapter:
    def __init__(self, tool_use_block):
        self.name = tool_use_block.name
        self.arguments = json.dumps(tool_use_block.input)


class _AnthropicToolCallAdapter:
    def __init__(self, tool_use_block):
        self.id = tool_use_block.id                          # needed by _append_tool_message
        self.function = _AnthropicFunctionAdapter(tool_use_block)
        self._raw_block = tool_use_block                     # retained for history reconstruction


class _AnthropicMessageAdapter:
    def __init__(self, response):
        self.content = next(
            (block.text for block in response.content if hasattr(block, "text")),
            None,
        )
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        self.tool_calls = [_AnthropicToolCallAdapter(b) for b in tool_blocks] or None
        self._raw_content = response.content                 # retained for history reconstruction


class _AnthropicUsageAdapter:
    def __init__(self, usage):
        self.prompt_tokens = usage.input_tokens
        self.completion_tokens = usage.output_tokens
        self.total_tokens = usage.input_tokens + usage.output_tokens
        # Cached tokens are billed at different rates (read ~0.1x, write ~1.25x
        # of the input rate) and are NOT included in input_tokens — Anthropic
        # reports the uncached remainder there. Surface them separately so cost
        # accounting can price each bucket correctly.
        #
        # Deliberately kept OUT of prompt_tokens/total_tokens: those feed the
        # MAX_TOKENS_PER_STEP throttle, and folding cache counts in would change
        # when that trips. Prompt caching is currently off (no cache_control is
        # set anywhere), so these read 0 until it is enabled.
        self.cache_read_input_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_input_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0


class _AnthropicChoiceAdapter:
    _STOP_REASON_MAP = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}

    def __init__(self, response):
        self.finish_reason = self._STOP_REASON_MAP.get(response.stop_reason, response.stop_reason)
        self.message = _AnthropicMessageAdapter(response)


class _AnthropicResponseAdapter:
    def __init__(self, response):
        self.choices = [_AnthropicChoiceAdapter(response)]
        self.usage = _AnthropicUsageAdapter(response.usage)


def _to_anthropic_tool_choice(tool_choice):
    """Convert OpenAI tool_choice value to Anthropic format."""
    if tool_choice is None or tool_choice == "none":
        return None
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        return {"type": "tool", "name": tool_choice["function"]["name"]}
    return {"type": "auto"}


def _convert_messages_to_anthropic(messages):
    """
    Convert an OpenAI-style message list to Anthropic (system, messages).

    Handles three special cases that arise in agentic loops:
    1. _AnthropicMessageAdapter objects appended after a previous call —
       reconstructed as assistant content blocks (text + tool_use).
    2. {"role": "tool", "tool_call_id": ...} results — converted to Anthropic
       tool_result blocks inside a "user" message.  Consecutive results are
       merged into a single user message as required by Anthropic's API.
    3. {"role": "system"} — extracted to the top-level system parameter.
    """
    system = None
    converted = []

    for msg in messages:
        # ── Previously returned assistant messages (Anthropic adapter objects) ──
        if isinstance(msg, _AnthropicMessageAdapter):
            content_blocks = []
            if msg.content:
                content_blocks.append({"type": "text", "text": msg.content})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.function.name,
                        "input": json.loads(tc.function.arguments),
                    })
            if not content_blocks:
                content_blocks = [{"type": "text", "text": ""}]
            converted.append({"role": "assistant", "content": content_blocks})
            continue

        # ── Standard dict messages ──
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            system = content
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block inside a user message.
            # Consecutive tool results MUST be merged into one user message.
            result_block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": str(content),
            }
            if (converted
                    and converted[-1]["role"] == "user"
                    and isinstance(converted[-1]["content"], list)):
                converted[-1]["content"].append(result_block)
            else:
                converted.append({"role": "user", "content": [result_block]})
            continue

        # ── Assistant message carrying tool calls as a PLAIN DICT ──
        # The shape a conversation restored from disk has: a stored message
        # cannot be an SDK adapter object, so it comes back as the OpenAI-style
        # dict. Without this branch it falls through to the text case below,
        # its tool_calls are silently dropped, and the tool_results that follow
        # have no tool_use to pair with — which the API rejects with
        # "unexpected tool_use_id ... in tool_result blocks".
        # Same blocks as the adapter branch above.
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if role == "assistant" and tool_calls:
            content_blocks = []
            if content:
                content_blocks.append({"type": "text", "text": content})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                try:
                    tool_input = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    # A malformed argument string must not sink the whole
                    # request; an empty input is recoverable, a 400 is not.
                    tool_input = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "input": tool_input,
                })
            if not content_blocks:
                content_blocks = [{"type": "text", "text": ""}]
            converted.append({"role": "assistant", "content": content_blocks})
            continue

        # Regular user / assistant text message
        converted.append({"role": role, "content": content})

    return system, converted


class _AnthropicCompletions:
    def __init__(self, adapter):
        self._adapter = adapter

    def create(self, model, messages, temperature=1.0, top_p=1.0,
               max_tokens=1024, frequency_penalty=None, presence_penalty=None,
               tools=None, tool_choice=None, **kwargs):

        system, filtered = _convert_messages_to_anthropic(messages)

        anthropic_tools = None
        if tools:
            anthropic_tools = []
            for t in tools:
                if t.get("type") == "function":
                    fn = t["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn["parameters"],
                    })

        # Anthropic rejects requests that specify both temperature and top_p.
        # Prefer temperature when it differs from the default; otherwise use top_p.
        params = dict(model=model, messages=filtered, max_tokens=max_tokens)
        if top_p != 1.0:
            params["top_p"] = top_p
        else:
            params["temperature"] = temperature
        if system:
            params["system"] = system
        if anthropic_tools:
            params["tools"] = anthropic_tools
        if tool_choice is not None:
            tc = _to_anthropic_tool_choice(tool_choice)
            if tc is not None:
                params["tool_choice"] = tc

        # Retry loop: transparently rotate to the next pool token when the
        # current one hits its daily cost cap. All other errors propagate.
        while True:
            # Snapshot the client that will actually service this attempt so a
            # concurrent rotation on another thread cannot misattribute the
            # 429 to the wrong pool entry.
            client_snapshot = self._adapter._client
            pool = self._adapter._pool
            used_token = pool.current()[1] if pool is not None else None
            try:
                return _AnthropicResponseAdapter(
                    client_snapshot.messages.create(**params)
                )
            except Exception as e:
                if pool is None or not _is_daily_cost_limit_error(e):
                    raise
                next_entry = pool.mark_dead_and_advance(used_token)
                if next_entry is None:
                    raise
                self._adapter._rebuild_underlying(next_entry[1])
                # loop and retry with the newly-selected token


class _AnthropicChatAdapter:
    def __init__(self, adapter):
        self.completions = _AnthropicCompletions(adapter)


class AnthropicOpenAIAdapter:
    """Wraps an ``Anthropic`` instance with an OpenAI-compatible interface.

    Optionally accepts a ``TokenPool`` and a ``client_factory`` to enable
    transparent rotation to the next pool token when the current one exhausts
    its daily cost limit. When both are omitted, behaves as a passive adapter.
    """

    def __init__(self, anthropic_client, pool=None, client_factory=None):
        self._client = anthropic_client
        self._pool = pool
        self._client_factory = client_factory
        self.chat = _AnthropicChatAdapter(self)

    def _rebuild_underlying(self, new_token):
        if self._client_factory is None:
            raise RuntimeError("client_factory required to rotate tokens")
        old_client = self._client
        # Concurrent rotations may briefly build two clients; the loser is GC'd.
        self._client = self._client_factory(new_token)
        close = getattr(old_client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# Token pool for gnaigpt daily-cost-limit failover.
# ---------------------------------------------------------------------------

def _is_daily_cost_limit_error(exc):
    """True for the specific 429 that means 'this token is done for the day'.

    Detects the gnaigpt proxy's per-user daily $ cap error, e.g.:
        "Error code: 429 - {'type': 'error', 'error': {'type': 'rate_limit_error',
         'message': 'Individual daily cost limit of 30.000000 reached...'}}"
    Deliberately narrow: short-window rate limits and unrelated 429s pass through.
    """
    if getattr(exc, "status_code", None) != 429:
        return False
    return "daily cost limit" in str(exc).lower()


class TokenPool:
    """Ordered, thread-safe pool of (label, token) pairs with sequential failover.

    Once a token is marked dead it stays dead for this pool's lifetime
    (process restart clears state). Callers use ``current()`` to read the
    active token and ``mark_dead_and_advance(dying_token)`` on 429s; the
    latter is a no-op when another thread has already advanced past the
    dying token, which is the correct behavior for concurrent 429s racing
    on the same key.
    """

    def __init__(self, entries):
        cleaned = [(str(label), tok) for label, tok in entries if tok]
        if not cleaned:
            raise ValueError("TokenPool requires at least one non-empty token")
        self._entries = cleaned
        self._index = 0
        self._dead = set()
        self._lock = threading.Lock()

    def current(self):
        with self._lock:
            return self._entries[self._index]

    def mark_dead_and_advance(self, dying_token):
        """Advance past ``dying_token`` if it is still current; else no-op.

        Returns the new active ``(label, token)`` or ``None`` when the whole
        pool is exhausted.
        """
        with self._lock:
            current_label, current_token = self._entries[self._index]
            if current_token != dying_token:
                # Another thread already rotated past this token; just report current.
                return self._entries[self._index]
            self._dead.add(self._index)
            for next_i in range(self._index + 1, len(self._entries)):
                if next_i not in self._dead:
                    self._index = next_i
                    new_label = self._entries[next_i][0]
                    print(f"⚠️  [TokenPool] '{current_label}' exhausted (daily cost limit) → rotated to '{new_label}'")
                    return self._entries[next_i]
            print(f"❌ [TokenPool] '{current_label}' exhausted — all {len(self._entries)} tokens dead")
            return None



# ---------------------------------------------------------------------------

access_token = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class LLM_helper:
    def __init__(self):
        self.proxies = {
            'http': 'http://proxy-dmz.intel.com:912',
            'https': 'http://proxy-dmz.intel.com:912',
        }
        self.client = None
        self.skills = None   # Dict[str, Skill] shared with log chatbot agent
        self.issue_categories = ["BSOD", "Yellow Bang (YB)", "Connectivity", "PPAG", 
                                "MLO", "Assert", "WRDS/WGDS/EWRD/SGOM", "TAS", "Roaming", 
                                "P2P", "DSM", "VLP/UHB/AFC", "UATS", "Unclassified"]

    @staticmethod
    def empty_usage() -> dict:
        return {
            "llm_calls": 0,
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    @staticmethod
    def accumulate_usage(target: dict, usage) -> dict:
        """Add one provider response usage object to a plain v5 usage dict."""
        if not isinstance(target, dict) or usage is None:
            return target

        def _get(name: str) -> int:
            try:
                if isinstance(usage, dict):
                    value = usage.get(name)
                else:
                    value = getattr(usage, name, 0)
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        prompt = _get("prompt_tokens") or _get("input_tokens")
        output = _get("completion_tokens") or _get("output_tokens")
        cache_read = _get("cache_read_input_tokens") or _get("cache_read_tokens")
        cache_write = _get("cache_creation_input_tokens") or _get("cache_write_tokens")
        target["llm_calls"] = int(target.get("llm_calls") or 0) + 1
        target["input_tokens"] = int(target.get("input_tokens") or 0) + prompt
        target["cache_read_tokens"] = int(target.get("cache_read_tokens") or 0) + cache_read
        target["cache_write_tokens"] = int(target.get("cache_write_tokens") or 0) + cache_write
        target["output_tokens"] = int(target.get("output_tokens") or 0) + output
        target["total_tokens"] = (
            int(target.get("total_tokens") or 0)
            + prompt + cache_read + cache_write + output
        )
        return target

    def set_up(self, gpt_token, gpt_url, model="gpt-4.1", classifitation_path=None,
               token_pool=None):
        # ``token_pool``: optional list of (label, token) tuples enabling
        # transparent rotation on daily-cost-limit 429s (Anthropic path only).
        # When supplied, ``gpt_token`` should equal the pool's first entry so
        # the initial client and pool head agree.
        if model.startswith("claude"):
            def _make_anthropic(tok):
                return Anthropic(
                    base_url=gpt_url,
                    auth_token=tok,
                    http_client=httpx.Client(proxy=None, verify=False, trust_env=False),
                )
            pool = TokenPool(token_pool) if token_pool else None
            initial_token = pool.current()[1] if pool is not None else gpt_token
            self.client = AnthropicOpenAIAdapter(
                _make_anthropic(initial_token),
                pool=pool,
                client_factory=_make_anthropic,
            )
            if pool is not None:
                print(f"🔑 [TokenPool] active token: '{pool.current()[0]}' ({len(pool._entries)} in pool)")
        else:
            self.client = openai.OpenAI(
                api_key=gpt_token,
                http_client=httpx.Client(proxy=None, verify=False, trust_env=False),
                base_url=gpt_url
            )
        self.model = model
        self.classifitation_path  = None
        if Path(classifitation_path).exists():
            self.classifitation_path = classifitation_path
        print("classifitation_path", classifitation_path, self.classifitation_path)

    def load_skills(self, data_dir: str) -> None:
        """
        Load diagnostic skills from the shared folder into self.skills.
        Called once at startup so the same skill objects are shared with
        WifiLogAgentSystem (avoids reading the files twice).
        """
        if data_dir and Path(data_dir).exists():
            print(f"🛠  Loading skills via LLM_helper from: {data_dir}")
            self.skills = load_skills_from_data_dir(data_dir)
            print(f"✅  {len(self.skills)} skills loaded.")
        else:
            print("⚠️  Skills data_dir not available – skills will fall back to built-ins.")
            self.skills = get_builtin_skills()
    
    
    def classify_issue(self, case_context: dict, usage_accumulator: dict = None):
        classify_prompt = "Analyze the content and classify into the most appropriate category based on the primary issue described:"
        # debug only:shared folder failed
        # if self.classifitation_path is not None:
        if 1==0:
            classification_info = helpers.load_module(self.classifitation_path,"classify_prompt_module" )
            self.issue_categories = classification_info.issue_categories
            classify_prompt += classification_info.CLASSIFY_PROMPT
            print("self.issue_categories", self.issue_categories)
        else:
            classify_prompt += """
            Categories and their indicators:
            - "BSOD": Blue Screen of Death, system crashes, dump files
            - "Yellow Bang (YB)": YB, Yellow Bang, Device lost, Device drop, hardware detection issues
            - "Connectivity": Connection issues, disconnect problems, network connectivity, DMA remapping
            - "PPAG": PPAG related issues
            - "MLO": MLO, Multi-Link Operation related issues
            - "Assert": Assert, assertion failures, software assertions
            - "WRDS/WGDS/EWRD/SGOM": WRDS, WGDS, EWRD, SGOM related issues
            - "TAS": TAS related issues
            - "Roaming": Roaming, roam related connectivity issues
            - "P2P": P2P, peer to peer connection issues
            - "DSM": DSM related issues
            - "VLP/UHB/AFC": VLP, UHB, AFC, function 3 related issues
            - "UATS": UATS related issues
            - "Unclassified": Issues that don't clearly fit into other categories

            Choose the category that best matches the PRIMARY issue described in the content. If multiple categories could apply, select the one that represents the main problem.

            """
        
        tool_schema = {
            "type": "function",
            "function": {
                "name": "classify_issue",
                "description": "Classify technical issues into specified categories",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "issue_type": {
                            "type": "string",
                            "enum": self.issue_categories,
                            "description": "Issue classification category"
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "Classification confidence score (0-1)"
                        },
                        "keywords_found": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keywords found in the content"
                        },
                    },
                    "required": ["issue_type", "confidence"]
                }
            }
        }
        
        user_content = f"""
        Case Description: {case_context}
        
        {classify_prompt}
        """
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{
                    "role": "user", 
                    "content": user_content
                }],
                tools=[tool_schema],  
                tool_choice={"type": "function", "function": {"name": "classify_issue"}},  # tool_choice
                temperature=0.1,
                max_tokens=300
            )
            if isinstance(usage_accumulator, dict):
                self.accumulate_usage(usage_accumulator, getattr(response, "usage", None))

            print("user_content", user_content)
            
            tool_calls = response.choices[0].message.tool_calls
            if tool_calls and len(tool_calls) > 0:
                function_call = tool_calls[0].function
                result = json.loads(function_call.arguments)
                return result
            else:
                content = response.choices[0].message.content
                return self._fallback_classification(content, case_context)
                
        except Exception as e:
            print(f"Classification failed: {e}")
            return {
                "issue_type": "Unclassified",
                "confidence": 0,
                "keywords_found": []
            }

    def analyze_desc(self, prompt_path, case_context: dict, return_usage: bool = False):
        
        prompt = helpers.load_module(prompt_path,"analyze_prompt_module" )
        
        system_content = (
           prompt.SYS_PROMPT
        )
        user_content = (
            f"""{case_context}"""
        )
        print("client:", self.client)

        operation_usage = self.empty_usage()
        classification_result = self.classify_issue(case_context, usage_accumulator=operation_usage)
        print("classification_result", classification_result)
        try:
            response = self.client.chat.completions.create(
                model=self.model,  
                messages=[
                    {
                        "role": "system",
                        "content": system_content
                    },
                    {
                        "role": "user", 
                        "content": user_content
                    }
                ],
                temperature=0.5,
                top_p=0.85,
                frequency_penalty=0.1,
                presence_penalty=0,
                max_tokens=1500,
                #stop=None
            )
            self.accumulate_usage(operation_usage, getattr(response, "usage", None))
            
            raw_output = response.choices[0].message.content
            print("raw_output:", raw_output)
            json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                result = json.loads(json_str)
                if classification_result:
                    result["Classification"] = classification_result
                else:
                    result["Classification"] = {
                        "issue_type": "Unclassified",
                        "confidence": 0,
                        "keywords_found": []
                    }
                print("json output:",type(result), result)
                output = result
            else:
                print("raw output:", raw_output)
                output = raw_output
        except Exception as e:
            print(f"Failed to make inference request: {e}")
            output = {}
        return (output, operation_usage) if return_usage else output
    
    def analyze_log(self, system_content, log=None, case_description=None):

        if case_description:
            user_content = dedent(f"""
                **Case Description Context:**
                {case_description}

                Use the above case description and the timestamp as context when analyzing the logs below.

                logs: {log}
            """).strip()
        else:
            user_content = f"logs: {log}"

        print("client:", self.client)
        try:

            response = self.client.chat.completions.create( #model=classification_info.tmp_model,
                model=self.model,  
                messages=[
                    {
                        "role": "system",
                        "content": system_content
                    },
                    {
                        "role": "user", 
                        "content": user_content
                    }
                ],
                temperature=0.2,
                top_p=0.9,
                frequency_penalty=0.1,
                presence_penalty=0,
                max_tokens=8000,
                #stop=None
            )

            """params = {
                "model": classification_info.tmp_model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user",   "content": user_content},
                ],
                "temperature": 0.2,
                "top_p": 0.9,
                "frequency_penalty": 0.1,
                "presence_penalty": 0,
                "stop": None
            }

            if getattr(classification_info, "max_token", None) is not None:
                params["max_tokens"] = classification_info.max_token
            """
            
            #response = self.client.chat.completions.create(**params)

            print(f"usage: {response.usage}")
            print(f"輸入 tokens: {response.usage.prompt_tokens}")
            print(f"輸出 tokens: {response.usage.completion_tokens}")
            print(f"總計 tokens: {response.usage.total_tokens}")
            print(f"是否被截斷: {response.choices[0].finish_reason}")

            print(f"response: {response}")
            raw_output = response.choices[0].message.content
            json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                result = json.loads(json_str)
                #print("json output:",type(result), result)
                return result
            else:
                #print("raw output:", raw_output)
                return raw_output
        except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, httpx.HTTPError, openai.OpenAIError) as e:
            print(f"Failed to make inference request: {e}")
            return {}

    def chat(self, messages: list, system_content: str = None) -> str:
        """
        Multi-turn chat: accepts full conversation history and returns LLM reply.
        
        Args:
            messages: list of {"role": "user"|"assistant", "content": "..."} dicts
            system_content: optional system prompt to prepend
        Returns:
            The assistant's reply as a string
        """
        api_messages = []
        if system_content:
            api_messages.append({"role": "system", "content": system_content})
        api_messages.extend(messages)

        # Debug: print full conversation history sent to LLM
        # print("\n" + "="*80)
        # print("[DEBUG chat] FULL API MESSAGES BEING SENT TO LLM:")
        # print("="*80)
        # for i, msg in enumerate(api_messages):
        #     role = msg['role']
        #     content = msg['content']
        #     preview = content[:500] + f"... ({len(content)} chars total)" if len(content) > 500 else content
        #     print(f"\n--- Message {i} | role: {role} | length: {len(content)} chars ---")
        #     print(preview)
        # print("\n" + "="*80 + "\n")

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                temperature=0.2,
                top_p=0.9,
                frequency_penalty=0.1,
                presence_penalty=0,
                max_tokens=8000,
                stop=None
            )

            print(f"[chat] usage: {response.usage}")
            print(f"[chat] finish_reason: {response.choices[0].finish_reason}")

            raw_output = response.choices[0].message.content
            return raw_output
        except Exception as e:
            print(f"[chat] Failed: {e}")
            raise
