from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from datetime import datetime, time, timedelta, timezone
from time import monotonic
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config_loader import AIConfig
from database.models import AIQuotaCounter, AIRecentResponse
from bot.ai.gemini_client import (
    GeminiRateLimitError,
    GeminiTemporaryError,
    TextGenerator,
)
from bot.ai.policy import (
    AIResult,
    is_explicit_banter_request,
    is_serious,
    parse_model_result,
    truncate_input,
)

logger = logging.getLogger(__name__)
PROJECT_QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")
USER_QUOTA_RETRIES = 4
MAX_TOTAL_REQUEST_SECONDS = 30
RETRY_BACKOFF_BASE_SECONDS = 0.25
RETRY_BACKOFF_MAX_SECONDS = 2.0


class QuotaUnavailable(Exception):
    """Raised when an atomic reservation cannot be completed safely."""


def _retry_delay_seconds(
    attempt: int,
    retry_after_seconds: float | None = None,
) -> float:
    exponential_cap = min(
        RETRY_BACKOFF_BASE_SECONDS * (2**attempt),
        RETRY_BACKOFF_MAX_SECONDS,
    )
    jittered = random.uniform(exponential_cap * 0.5, exponential_cap * 1.5)
    if retry_after_seconds is not None:
        return max(jittered, retry_after_seconds)
    return jittered


def _utc_naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _user_day_start(now: datetime, timezone_name: str) -> datetime:
    local_now = now.astimezone(ZoneInfo(timezone_name))
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=local_now.tzinfo)
    return _utc_naive(local_start)


def _project_day_start(now: datetime) -> datetime:
    local_now = now.astimezone(PROJECT_QUOTA_TIMEZONE)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=PROJECT_QUOTA_TIMEZONE)
    return _utc_naive(local_start)


async def reserve_quota(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    config: AIConfig,
    user_timezone: str,
    *,
    bypass_user_limits: bool = False,
    estimated_tokens: int = 1,
    now: datetime | None = None,
) -> bool:
    """Atomically reserve user and project minute/day buckets in one SQLite txn."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    minute_start = _utc_naive(instant.replace(second=0, microsecond=0))
    user_day = _user_day_start(instant, user_timezone)
    project_day = _project_day_start(instant)
    if estimated_tokens <= 0:
        raise ValueError("estimated_tokens must be positive")
    if estimated_tokens > config.global_tokens_per_minute:
        logger.info("AI quota rejected scope=project window=token_minute")
        return False
    buckets = [
        ("global", "project", "day", project_day, config.global_daily_limit, 1),
        ("global", "project", "minute", minute_start, config.global_per_minute_limit, 1),
        (
            "global", "project", "token_minute", minute_start,
            config.global_tokens_per_minute, estimated_tokens,
        ),
    ]
    if not bypass_user_limits:
        buckets[0:0] = [
            ("user", str(user_id), "day", user_day, config.user_daily_limit, 1),
            ("user", str(user_id), "minute", minute_start, config.user_per_minute_limit, 1),
        ]

    for attempt in range(USER_QUOTA_RETRIES):
        try:
            async with session_factory() as session:
                if session.bind is None or session.bind.dialect.name != "sqlite":
                    raise QuotaUnavailable(
                        "Atomic Gemini quota reservations require SQLite"
                    )
                async with session.begin():
                    # Acquire the SQLite write lock before inspecting/reserving any bucket.
                    await session.execute(text("BEGIN IMMEDIATE"))
                    for scope, scope_id, window, start, limit, increment in buckets:
                        statement = (
                            insert(AIQuotaCounter)
                            .values(
                                scope=scope,
                                scope_id=scope_id,
                                window=window,
                                window_start=start,
                                count=increment,
                            )
                            .on_conflict_do_update(
                                index_elements=[
                                    "scope", "scope_id", "window", "window_start"
                                ],
                                set_={"count": AIQuotaCounter.count + increment},
                                where=AIQuotaCounter.count <= limit - increment,
                            )
                        )
                        result = await session.execute(statement)
                        if result.rowcount != 1:
                            raise _QuotaLimitReached
            return True
        except _QuotaLimitReached:
            quota_scope = "project" if scope == "global" else scope
            logger.info(
                "AI quota rejected scope=%s window=%s",
                quota_scope,
                window,
            )
            return False
        except OperationalError as exc:
            if "locked" not in str(exc.orig).casefold() or attempt + 1 == USER_QUOTA_RETRIES:
                logger.error("Atomic AI quota reservation failed (%s)", type(exc).__name__)
                raise QuotaUnavailable("Could not reserve AI quota safely") from exc
            await asyncio.sleep(0.025 * (2**attempt))
        except SQLAlchemyError as exc:
            logger.error("Atomic AI quota reservation failed (%s)", type(exc).__name__)
            raise QuotaUnavailable("Could not reserve AI quota safely") from exc
    raise QuotaUnavailable("Could not reserve AI quota safely")


class _QuotaLimitReached(Exception):
    pass


async def _is_recent_joke_repeat(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    response: str,
    repeat_window_minutes: int,
) -> bool:
    normalized = re.sub(r"\W+", " ", response.casefold()).strip()
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    try:
        async with session_factory() as session:
            if session.bind is None or session.bind.dialect.name != "sqlite":
                raise QuotaUnavailable("Repeat protection requires SQLite")
            async with session.begin():
                await session.execute(text("BEGIN IMMEDIATE"))
                repeat = await session.scalar(
                    select(AIRecentResponse.id)
                    .where(
                        AIRecentResponse.telegram_id == user_id,
                        AIRecentResponse.response_hash == fingerprint,
                        AIRecentResponse.created_at
                        > (now - timedelta(minutes=repeat_window_minutes)).replace(tzinfo=None),
                    )
                    .limit(1)
                )
                if repeat is not None:
                    return True
                session.add(
                    AIRecentResponse(
                        telegram_id=user_id,
                        response_hash=fingerprint,
                        created_at=now.replace(tzinfo=None),
                    )
                )
        return False
    except SQLAlchemyError as exc:
        logger.warning("AI joke repeat protection unavailable (%s)", type(exc).__name__)
        raise QuotaUnavailable("Could not check recent joke safely") from exc


class AIGateway:
    def __init__(
        self,
        client: TextGenerator | None,
        session_factory: async_sessionmaker[AsyncSession],
        config: AIConfig,
        user_timezone: str,
        owner_ids: set[int] | tuple[int, ...] | list[int] = (),
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._config = config
        self._user_timezone = user_timezone
        self._owner_ids = frozenset(owner_ids)
        self._semaphore = asyncio.Semaphore(4)

    async def generate(
        self,
        user_id: int,
        user_text: str,
        *,
        allow_humor: bool,
        humor_style: str = "memey",
    ) -> AIResult | None:
        if (
            not self._config.enabled
            or not self._config.project_limits_verified
            or self._client is None
        ):
            return None
        if is_serious(user_text):
            return AIResult(
                intent="ordinary_reply",
                response="Понял, отвечу без шуток. Если нужен раздел бота, открой /help.",
            )
        bounded_text = truncate_input(user_text, self._config.max_input_chars)
        max_attempts = self._config.retries + 1
        total_deadline_seconds = min(
            self._config.timeout_seconds * max_attempts,
            MAX_TOTAL_REQUEST_SECONDS,
        )
        total_started = monotonic()
        try:
            async with asyncio.timeout(total_deadline_seconds):
                async with self._semaphore:
                    for attempt in range(max_attempts):
                        try:
                            estimated_tokens = (
                                len(bounded_text.encode("utf-8"))
                                + self._config.prompt_token_reserve
                                + self._config.joke_max_output_tokens
                            )
                            reserved = await reserve_quota(
                                self._session_factory,
                                user_id,
                                self._config,
                                self._user_timezone,
                                bypass_user_limits=user_id in self._owner_ids,
                                estimated_tokens=estimated_tokens,
                            )
                        except QuotaUnavailable:
                            return None
                        if not reserved:
                            return None
                        request_started = monotonic()
                        try:
                            async with asyncio.timeout(self._config.timeout_seconds):
                                raw = await self._client.generate(
                                    bounded_text,
                                    max_output_tokens=self._config.joke_max_output_tokens,
                                    humor_style=(
                                        humor_style
                                        if allow_humor
                                        and humor_style in {"memey", "gentle"}
                                        else "neutral"
                                    ),
                                )
                        except GeminiRateLimitError as exc:
                            retryable = exc.category == "rate_limit"
                            logger.warning(
                                "Gemini provider limit handled category=%s "
                                "retrying=%s attempt=%d/%d",
                                exc.category,
                                retryable and attempt < self._config.retries,
                                attempt + 1,
                                max_attempts,
                            )
                            if not retryable or attempt >= self._config.retries:
                                return None
                            delay = _retry_delay_seconds(
                                attempt,
                                exc.retry_after_seconds,
                            )
                            remaining = total_deadline_seconds - (
                                monotonic() - total_started
                            )
                            if delay >= remaining:
                                logger.warning(
                                    "Gemini retry skipped reason=deadline "
                                    "attempt=%d/%d",
                                    attempt + 1,
                                    max_attempts,
                                )
                                return None
                            await asyncio.sleep(delay)
                            continue
                        except (GeminiTemporaryError, asyncio.TimeoutError) as exc:
                            logger.warning(
                                "Gemini temporary failure attempt=%d/%d retrying=%s "
                                "exception_type=%s elapsed_ms=%d",
                                attempt + 1,
                                max_attempts,
                                attempt + 1 < max_attempts,
                                type(exc).__name__,
                                int((monotonic() - request_started) * 1000),
                            )
                            if attempt < self._config.retries:
                                retry_after = (
                                    exc.retry_after_seconds
                                    if isinstance(exc, GeminiTemporaryError)
                                    else None
                                )
                                delay = _retry_delay_seconds(attempt, retry_after)
                                remaining = total_deadline_seconds - (
                                    monotonic() - total_started
                                )
                                if delay >= remaining:
                                    logger.warning(
                                        "Gemini retry skipped reason=deadline "
                                        "attempt=%d/%d",
                                        attempt + 1,
                                        max_attempts,
                                    )
                                    return None
                                await asyncio.sleep(delay)
                                continue
                            return None
                        except Exception as exc:
                            logger.warning(
                                "Gemini request unavailable exception_type=%s elapsed_ms=%d",
                                type(exc).__name__,
                                int((monotonic() - request_started) * 1000),
                            )
                            return None

                        elapsed_ms = int((monotonic() - request_started) * 1000)
                        logger.info(
                            "Gemini request completed attempt=%d/%d elapsed_ms=%d",
                            attempt + 1,
                            max_attempts,
                            elapsed_ms,
                        )
                        result = parse_model_result(raw)
                        if result is None:
                            logger.warning(
                                "Gemini response failed local validation elapsed_ms=%d",
                                elapsed_ms,
                            )
                            return None
                        if result.intent == "joke_or_banter" and (
                            not allow_humor or not is_explicit_banter_request(user_text)
                        ):
                            return AIResult(
                                intent="ordinary_reply",
                                response=(
                                    "Могу помочь с вопросами по боту. "
                                    "Открой /help для списка разделов."
                                ),
                            )
                        if result.intent == "joke_or_banter":
                            try:
                                if await _is_recent_joke_repeat(
                                    self._session_factory,
                                    user_id,
                                    result.response,
                                    self._config.joke_repeat_window_minutes,
                                ):
                                    return AIResult(
                                        intent="ordinary_reply",
                                        response=(
                                            "Похоже, я уже рассказывал эту шутку. "
                                            "Давай сменим тему."
                                        ),
                                    )
                            except QuotaUnavailable:
                                return None
                        return result
        except asyncio.TimeoutError:
            logger.warning(
                "Gemini total request deadline exceeded elapsed_ms=%d",
                int((monotonic() - total_started) * 1000),
            )
            return None


async def clean_old_quota_counters(
    session: AsyncSession,
    retention_days: int,
    *,
    now: datetime | None = None,
) -> None:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    await session.execute(
        delete(AIQuotaCounter).where(AIQuotaCounter.window_start < _utc_naive(cutoff))
    )
