import requests
import json
import re
import urllib3
import openai
import httpx
from pathlib import Path
from utils import helpers


access_token = None
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class LLM_helper:
    def __init__(self):
        self.proxies = {
            'http': 'http://proxy-dmz.intel.com:912',
            'https': 'http://proxy-dmz.intel.com:912',
        }
        self.client = None
        self.issue_categories = ["BSOD", "Yellow Bang (YB)", "Connectivity", "PPAG", 
                                "MLO", "Assert", "WRDS/WGDS/EWRD/SGOM", "TAS", "Roaming", 
                                "P2P", "DSM", "VLP/UHB/AFC", "UATS", "Unclassified"]

    def set_up(self, expertgpt_token, expertgpt_url, model="gpt-4.1",classifitation_path=None):
        openai.api_key = expertgpt_token
        self.client = openai.OpenAI(
            api_key=expertgpt_token,
            http_client=httpx.Client(proxy=None, verify=False, trust_env=False),
            base_url=expertgpt_url
        )
        self.model = model
        self.classifitation_path  = None
        if Path(classifitation_path).exists():
            self.classifitation_path = classifitation_path
        print("classifitation_path", classifitation_path, self.classifitation_path)
    
    
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
                stop=None
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
        except requests.exceptions.RequestException as e:
            print(f"Failed to make inference request: {e}")
            return {}
    
    def analyze_log(self, system_content, log=None):

        user_content = (
            f"""logs: {log}\n"""
        )
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
                stop=None
            )
            #"""

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
        except requests.exceptions.RequestException as e:
            print(f"Failed to make inference request: {e}")
            return {}


#helper = LLM_helper()
#helper.set_up("key")
#print(helper.client)
