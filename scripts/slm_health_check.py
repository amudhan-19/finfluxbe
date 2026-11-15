# @file    scripts/slm_health_check.py
#
# Stand-alone smoke test for the SLM chat gateway. Verifies that the
# configured URL + API key + project/template ids can round-trip a tiny
# JSON payload without crashing the FinFlux backend. Designed for CI
# health probes and manual config validation.
#
# Run:
#     python -m scripts.slm_health_check
#
# Output is intentionally terse and *never* prints the API key.

from __future__ import annotations
import asyncio
import logging

from app.services.slm.slm_client import SlmChatMessage, slm_chat_request
from app.services.slm.slm_config import load_slm_config
from app.services.slm.slm_json_utils import parse_loose_json_from_llm_text


_HEALTH_SYSTEM_PROMPT = "Reply with compact JSON only. No prose, no markdown."
_HEALTH_USER_PROMPT   = 'Return exactly {"status":"ok"}'


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    cfg = load_slm_config()

    print(f"URL configured     : {_yes_no(bool(cfg.chat_url))}")
    print(f"API key configured : {_yes_no(bool(cfg.api_key))}")
    print(f"Project id set     : {_yes_no(bool(cfg.project_id))}")
    print(f"Template id set    : {_yes_no(bool(cfg.template_id))}")

    if not cfg.is_configured:
        print("RESULT             : NOT_CONFIGURED")
        print("Set SLM_CHAT_URL and SLM_CHAT_API_KEY in .env to enable AI.")
        return 2

    messages = [
        SlmChatMessage(role="system", content=_HEALTH_SYSTEM_PROMPT),
        SlmChatMessage(role="user",   content=_HEALTH_USER_PROMPT),
    ]
    result = await slm_chat_request(messages, cfg)

    print(f"HTTP status        : {result.http_status}")
    print(f"Finish reason      : {result.finish_reason or '-'}")
    print(f"Result ok          : {_yes_no(result.ok)}")

    if not result.ok or not result.text:
        code = result.error.code if result.error else "UNKNOWN"
        print(f"RESULT             : FAILED ({code})")
        return 1

    parsed = parse_loose_json_from_llm_text(result.text)
    if not isinstance(parsed, dict) or parsed.get("status") != "ok":
        print(f"Parsed result      : {parsed!r}")
        print("RESULT             : UNEXPECTED_PAYLOAD")
        return 1

    print(f"Parsed result      : {parsed}")
    print("RESULT             : OK")
    return 0


def main() -> None:
    code = asyncio.run(_run())
    raise SystemExit(code)


if __name__ == "__main__":
    main()
