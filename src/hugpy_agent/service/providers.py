"""Wire adapters for configured APIs, using the agent's existing Gateway seam."""
import json
import os
import urllib.error
import urllib.request

from ..gateway import Gateway, ChatResult, estimate_tokens


class ProviderGateway(Gateway):
    session_signals = False   # third-party API: no X-Hugpy-* identity, no lease

    def __init__(self, profile, timeout=300):
        self.profile = profile
        super().__init__(profile["base_url"], os.environ.get(profile.get("api_key_env", ""), ""),
                         profile["model"], timeout)

    def resolve(self):
        base = self.profile["base_url"].rstrip("/")
        return base + "/chat/completions", base + "/models"

    def context_length(self, model=None, fallback=8192):
        return self.profile["context_length"]

    def build_payload(self, messages, model=None, temperature=0.2, max_tokens=1024, stream=True, tools=None):
        # Prompted tool calling is shared across all API protocols. No fleet-only
        # fields or native tool-call IDs are sent to unrelated providers.
        p = dict(self.profile.get("parameters", {}))
        p.update(model=model or self.model, messages=messages, stream=bool(stream))
        p[self.profile.get("token_parameter", "max_tokens")] = max_tokens
        return p

    def chat(self, messages, model=None, temperature=0.2, max_tokens=1024,
             stream=True, tools=None, on_delta=None, retries=0, timeout=None):
        protocol = self.profile["protocol"]
        if protocol == "openai-chat":
            return super().chat(messages, model, temperature, max_tokens, stream, None,
                                on_delta, retries, timeout)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        payload = dict(self.profile.get("parameters", {}))
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
            if self.api_key:
                headers["x-api-key"] = self.api_key
            payload.update(model=model or self.model, max_tokens=max_tokens, stream=False,
                           system="\n\n".join(m["content"] for m in messages if m["role"] == "system"),
                           messages=[m for m in messages if m["role"] != "system"])
            route = "/messages"
        else:
            if self.api_key:
                headers["Authorization"] = "Bearer " + self.api_key
            payload.update(model=model or self.model, input=messages,
                           max_output_tokens=max_tokens, stream=False, store=False)
            route = "/responses"
        req = urllib.request.Request(self.profile["base_url"].rstrip("/") + route,
                                     data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                doc = json.load(response)
            if protocol == "anthropic":
                text = "".join(b.get("text", "") for b in doc.get("content", []) if b.get("type") == "text")
            else:
                text = "".join(b.get("text", "") for item in doc.get("output", [])
                               if item.get("type") == "message" for b in item.get("content", [])
                               if b.get("type") == "output_text")
            if not text:
                return ChatResult(ok=False, error="Provider returned no text (check model/output token budget)")
            if on_delta:
                on_delta(text)
            return ChatResult(ok=True, text=text, est_tokens=estimate_tokens(text))
        except urllib.error.HTTPError as exc:
            return ChatResult(ok=False, error="Provider returned HTTP %s" % exc.code)
        except (OSError, ValueError) as exc:
            return ChatResult(ok=False, error="Provider request failed: " + type(exc).__name__)


class FleetGateway(Gateway):
    """Interactive Station sessions participate in normal Fleet admission/eviction."""
    def __init__(self, profile, cfg):
        self.profile = profile
        super().__init__(cfg.base, cfg.api_key, cfg.model, cfg.timeout, no_think=cfg.no_think)

    def build_payload(self, *args, **kwargs):
        payload = super().build_payload(*args, **kwargs)
        payload["no_makeroom"] = not self.profile.get("allow_eviction", True)
        payload.update(self.profile.get("parameters", {}))
        return payload


def gateway(profile, cfg):
    if profile["protocol"] == "hugpy":
        return FleetGateway(profile, cfg)
    return ProviderGateway(profile, cfg.timeout)
