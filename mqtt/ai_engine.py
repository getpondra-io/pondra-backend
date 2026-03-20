"""
AquaMind AI Engine
──────────────────
Supports:
  - Managed: AquaMind's Claude API key
  - BYOK Claude: user's own Anthropic key
  - BYOK OpenAI: GPT-4 / GPT-4o
  - BYOK Custom: any REST endpoint that follows AquaMind's schema

All providers share the same interface: analyse(enriched) → AIDecision
"""

import json
import structlog
from datetime import datetime
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import anthropic
import httpx

from config.settings import get_settings
from models.sensor import (
    EnrichedReading, AIDecision, ActionType, Severity, ThresholdConfig
)

settings = get_settings()
log = structlog.get_logger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are AquaMind's autonomous aquaculture AI agent.

You receive real-time water quality sensor data from fish and shrimp farms.
Your job is to:
1. Analyse the current water quality against species thresholds
2. Check for dangerous trends in the past hour
3. Decide what actions (if any) to take
4. Return a structured JSON decision

CRITICAL RULES:
- Only trigger actions when genuinely necessary — avoid alert fatigue
- Always explain your reasoning clearly
- Consider feeding history and water change history
- For CRITICAL issues: act immediately and set alert_message
- For WARNING issues: act preventively
- For OK status: return no_action

You must respond with ONLY valid JSON, no other text. Schema:
{
  "severity": "ok" | "warning" | "critical",
  "actions": ["aerator_on", "feed_reduce", ...],
  "reasoning": "Clear explanation of your decision",
  "duration_min": 25,
  "feed_adjustment_pct": -20,
  "alert_message": "...",
  "confidence": 0.95
}

Available actions:
  aerator_on, aerator_off, pump_on, pump_off,
  heater_on, heater_off, feed, feed_reduce, feed_skip,
  alert, water_change, no_action
"""


def _build_user_prompt(enriched: EnrichedReading) -> str:
    """Build the full context prompt from enriched reading."""
    r = enriched.reading
    t = enriched.thresholds

    trend_summary = "No history available."
    if enriched.history_1h:
        nh3_vals = [h.nh3 for h in enriched.history_1h if h.nh3 is not None]
        do_vals = [h.do for h in enriched.history_1h if h.do is not None]
        if nh3_vals:
            trend_summary = (
                f"NH3 over last hour: min={min(nh3_vals):.2f}, "
                f"max={max(nh3_vals):.2f}, "
                f"latest={nh3_vals[0]:.2f} ppm"
            )
        if do_vals:
            trend_summary += (
                f" | DO: min={min(do_vals):.2f}, "
                f"max={max(do_vals):.2f}, "
                f"latest={do_vals[0]:.2f} mg/L"
            )

    fed_str = "Unknown"
    if enriched.last_fed_seconds_ago is not None:
        hrs = enriched.last_fed_seconds_ago // 3600
        mins = (enriched.last_fed_seconds_ago % 3600) // 60
        fed_str = f"{hrs}h {mins}m ago"

    wc_str = "Unknown"
    if enriched.last_water_change_seconds_ago is not None:
        days = enriched.last_water_change_seconds_ago // 86400
        wc_str = f"{days} days ago"

    return f"""
FARM: {enriched.farm_name}
SPECIES: {enriched.species} | GROWTH STAGE: {enriched.growth_stage}

CURRENT READINGS:
  Dissolved O2 : {r.do} mg/L    (threshold: >{t.do_min})
  pH           : {r.ph}          (threshold: {t.ph_min}-{t.ph_max})
  Ammonia NH3  : {r.nh3} ppm    (threshold: <{t.nh3_max})
  Temperature  : {r.temp}C     (threshold: {t.temp_min}-{t.temp_max})
  Salinity     : {r.salinity} ppt
  Turbidity    : {r.turbidity} NTU

1-HOUR TREND:
  {trend_summary}

CONTEXT:
  Last feeding        : {fed_str}
  Last water change   : {wc_str}
  Readings in last 1h : {len(enriched.history_1h)}

Analyse this data and return your JSON decision.
""".strip()


# ── Base provider ─────────────────────────────────────────────────────────────

class BaseAIProvider:
    name: str = "base"
    model: str = ""

    async def call(self, prompt: str) -> str:
        raise NotImplementedError

    def _parse_response(self, raw: str, farm_id: str) -> AIDecision:
        """Parse AI JSON response into AIDecision."""
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip()
            data = json.loads(clean)
        except json.JSONDecodeError:
            log.error("ai.parse_error", raw=raw[:200])
            return self._fallback_decision(farm_id, "AI returned invalid JSON")

        try:
            actions = [ActionType(a) for a in data.get("actions", ["no_action"])]
        except ValueError:
            actions = [ActionType.NO_ACTION]

        try:
            severity = Severity(data.get("severity", "ok"))
        except ValueError:
            severity = Severity.OK

        return AIDecision(
            farm_id=farm_id,
            severity=severity,
            actions=actions,
            reasoning=data.get("reasoning", ""),
            duration_min=data.get("duration_min"),
            feed_adjustment_pct=data.get("feed_adjustment_pct"),
            alert_message=data.get("alert_message"),
            confidence=float(data.get("confidence", 1.0)),
            ai_provider=self.name,
            ai_model=self.model,
        )

    def _fallback_decision(self, farm_id: str, reason: str) -> AIDecision:
        return AIDecision(
            farm_id=farm_id,
            severity=Severity.OK,
            actions=[ActionType.NO_ACTION],
            reasoning=f"Fallback (AI error): {reason}",
            ai_provider=self.name,
            ai_model=self.model,
        )


# ── Claude provider ───────────────────────────────────────────────────────────

class ClaudeProvider(BaseAIProvider):
    name = "claude"

    def __init__(self, api_key: str, model: Optional[str] = None):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model or settings.anthropic_model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(anthropic.APIError),
    )
    async def call(self, prompt: str) -> str:
        message = await self.client.messages.create(
            model=self.model,
            max_tokens=settings.ai_max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text


# ── OpenAI provider ───────────────────────────────────────────────────────────

class OpenAIProvider(BaseAIProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def call(self, prompt: str) -> str:
        import openai
        response = await self.client.chat.completions.create(
            model=self.model,
            max_tokens=settings.ai_max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content


# ── Custom REST provider ──────────────────────────────────────────────────────

class CustomRESTProvider(BaseAIProvider):
    name = "custom"

    def __init__(self, endpoint: str, api_key: str, model: str = "custom"):
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10))
    async def call(self, prompt: str) -> str:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "system": SYSTEM_PROMPT,
                    "prompt": prompt,
                    "max_tokens": settings.ai_max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("content") or data.get("text") or json.dumps(data)


# ── Rule-based fallback ───────────────────────────────────────────────────────

class RuleBasedProvider(BaseAIProvider):
    name = "rule_based"
    model = "threshold_v1"

    async def call(self, prompt: str) -> str:
        return json.dumps({
            "severity": "ok",
            "actions": ["no_action"],
            "reasoning": "Rule-based analysis: all parameters within acceptable range.",
            "confidence": 0.8,
        })

    async def analyse_direct(self, enriched: EnrichedReading) -> AIDecision:
        severity, issues = enriched.thresholds.evaluate(enriched.reading)
        actions = [ActionType.NO_ACTION]
        alert_message = None

        if severity == Severity.CRITICAL:
            actions = [ActionType.AERATOR_ON, ActionType.ALERT]
            alert_message = f"Critical: {'; '.join(issues)}"
        elif severity == Severity.WARNING:
            actions = [ActionType.AERATOR_ON]

        return AIDecision(
            farm_id=enriched.reading.farm_id,
            severity=severity,
            actions=actions,
            reasoning=f"Rule-based: {'; '.join(issues) if issues else 'All parameters OK'}",
            alert_message=alert_message,
            confidence=0.75,
            ai_provider=self.name,
            ai_model=self.model,
        )


# ── AI Engine (orchestrator) ──────────────────────────────────────────────────

class AIEngine:
    def __init__(self):
        self._managed_provider: Optional[BaseAIProvider] = None
        if settings.anthropic_api_key:
            self._managed_provider = ClaudeProvider(settings.anthropic_api_key)
            log.info("ai.managed_provider_ready", model=settings.anthropic_model)
        else:
            log.warning("ai.no_managed_key", fallback="rule_based")

        self._rule_based = RuleBasedProvider()

    async def analyse(self, enriched: EnrichedReading) -> AIDecision:
        farm_id = enriched.reading.farm_id
        provider = await self._get_provider_for_farm(enriched)

        if provider is None:
            log.info("ai.rule_based_fallback", farm_id=farm_id)
            return await self._rule_based.analyse_direct(enriched)

        prompt = _build_user_prompt(enriched)

        try:
            raw_response = await provider.call(prompt)
            decision = provider._parse_response(raw_response, farm_id)
            log.info("ai.decision",
                farm_id=farm_id,
                provider=provider.name,
                severity=decision.severity,
                actions=decision.actions,
            )
            return decision

        except Exception as e:
            log.error("ai.call_failed", farm_id=farm_id, error=str(e))
            return await self._rule_based.analyse_direct(enriched)

    async def _get_provider_for_farm(self, enriched: EnrichedReading) -> Optional[BaseAIProvider]:
        if self._managed_provider:
            return self._managed_provider
        return None

    def get_byok_provider(
        self,
        ai_provider: str,
        api_key: str,
        custom_endpoint: Optional[str] = None,
    ) -> BaseAIProvider:
        if ai_provider == "claude":
            return ClaudeProvider(api_key)
        elif ai_provider == "openai":
            return OpenAIProvider(api_key)
        elif ai_provider == "custom" and custom_endpoint:
            return CustomRESTProvider(custom_endpoint, api_key)
        else:
            log.warning("ai.unknown_provider", provider=ai_provider)
            return self._rule_based
