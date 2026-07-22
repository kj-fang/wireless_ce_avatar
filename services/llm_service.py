from logging import log

import requests
import json
import re
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

        # Regular user / assistant text message
        converted.append({"role": role, "content": content})

    return system, converted


class _AnthropicCompletions:
    def __init__(self, client):
        self._client = client

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

        return _AnthropicResponseAdapter(self._client.messages.create(**params))


class _AnthropicChatAdapter:
    def __init__(self, client):
        self.completions = _AnthropicCompletions(client)


class AnthropicOpenAIAdapter:
    """Wraps an ``Anthropic`` instance with an OpenAI-compatible interface."""

    def __init__(self, anthropic_client):
        self._client = anthropic_client
        self.chat = _AnthropicChatAdapter(anthropic_client)


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

    def set_up(self, gpt_token, gpt_url, model="gpt-4.1", classifitation_path=None):
        if model.startswith("claude"):
            self.client = AnthropicOpenAIAdapter(Anthropic(
                base_url=gpt_url,
                auth_token=gpt_token,
                http_client=httpx.Client(proxy=None, verify=False, trust_env=False),
            ))
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
    
    
    def classify_issue(self, case_context: dict):
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

    def analyze_desc(self, prompt_path, case_context: dict):
        
        prompt = helpers.load_module(prompt_path,"analyze_prompt_module" )
        
        system_content = (
           prompt.SYS_PROMPT
        )
        user_content = (
            f"""{case_context}"""
        )
        print("client:", self.client)

        classification_result = self.classify_issue(case_context)
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
                return result
            else:
                print("raw output:", raw_output)
                return raw_output
        except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError, httpx.HTTPError, openai.OpenAIError) as e:
            print(f"Failed to make inference request: {e}")
            return {}
    
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

            # Accumulate token usage on this helper instance so callers (e.g.
            # the ACE CLI, which uses a dedicated LLM_helper per run) can report
            # how many tokens a whole run consumed. Lazily initialised; safe
            # when the provider omits usage.
            u = getattr(response, "usage", None)
            if u is not None:
                acc = getattr(self, "_ace_usage", None)
                if acc is None:
                    acc = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
                    self._ace_usage = acc
                acc["prompt"] += getattr(u, "prompt_tokens", 0) or 0
                acc["completion"] += getattr(u, "completion_tokens", 0) or 0
                acc["total"] += getattr(u, "total_tokens", 0) or 0
                acc["calls"] += 1

            raw_output = response.choices[0].message.content
            return raw_output
        except Exception as e:
            print(f"[chat] Failed: {e}")
            raise

    def get_usage(self) -> dict:
        """Cumulative token usage of every chat() call on this instance.
        Returns {"prompt", "completion", "total", "calls"}."""
        return dict(getattr(self, "_ace_usage",
                            {"prompt": 0, "completion": 0, "total": 0, "calls": 0}))

    def reset_usage(self) -> None:
        """Zero the cumulative token counters (call before a run to measure it
        in isolation)."""
        self._ace_usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
