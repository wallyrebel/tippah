"""OpenAI client for AP-style article rewriting."""

from __future__ import annotations

import json
import re
import time
from typing import Optional

from openai import OpenAI

from rss_to_wp.utils import get_logger

logger = get_logger("rewriter.openai")

# System prompt for AP-style rewriting
AP_STYLE_PROMPT = """You are a professional news editor who rewrites press releases and articles into AP (Associated Press) style news articles.

RULES:
1. Write in objective, third-person voice
2. Use short, punchy sentences and paragraphs
3. Lead with the most newsworthy information (inverted pyramid)
4. Attribute all claims to sources
5. Use active voice whenever possible
6. Avoid editorializing or adding opinions
7. Do NOT fabricate facts, quotes, or details not present in the source
8. If information is missing, do not invent it
9. Keep the article factual and concise
10. Use proper AP style for numbers, dates, titles, etc.

OUTPUT FORMAT:
You must respond with valid JSON in this exact format:
{
    "headline": "Short, compelling headline in AP style",
    "excerpt": "One to two sentence summary for preview",
    "body": "Full article body in HTML format with <p> tags for paragraphs"
}

IMPORTANT:
- The body should be 3-6 paragraphs
- Use <p> tags to wrap each paragraph
- Do NOT include the headline in the body
- Do NOT include any markdown - use HTML only
"""

WEEKLY_COLUMN_PROMPT = """You are an elite syndicated columnist writing for a broad newspaper audience.

OUTPUT FORMAT:
Respond with valid JSON only:
{
    "headline": "Compelling column headline",
    "excerpt": "One to two sentence teaser",
    "body": "Column body in HTML with <p> tags",
    "image_query": "2 to 6 word photo search phrase"
}

RULES:
1. Write polished, publication-ready prose.
2. Use only facts from the provided context; do not invent facts, quotes, scores, or names.
3. If context is thin, acknowledge uncertainty instead of fabricating details.
4. Keep body between 5 and 9 short paragraphs.
5. Do not include markdown; HTML <p> only.
6. Keep the tone suitable for a mainstream syndicated newspaper column.
"""


def _column_style_brief(column_type: str) -> str:
    """Return style guidance for each configured weekly column type."""
    if column_type == "christian":
        return (
            "Write as a nationally syndicated Christian religion columnist with doctorate-level "
            "theological literacy. Blend biblical grounding, pastoral empathy, civic relevance, "
            "and practical moral reflection without partisan ranting."
        )
    if column_type == "human_interest":
        return (
            "Write a classic human-interest syndicated column with narrative momentum, warmth, "
            "wit, and a memorable ending. The cadence may be inspired by old-school radio/newspaper "
            "storytelling, but must remain original and not imitate any specific writer."
        )
    if column_type == "sports":
        return (
            "Write a high-end national sports column with sharp observation, personality, humor, "
            "and clear argument. Focus on one current national sports topic and stay factual."
        )
    return (
        "Write an original, high-quality syndicated column with strong voice, factual grounding, "
        "and clear structure."
    )


class OpenAIRewriter:
    """Client for rewriting articles using OpenAI."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5-mini",
        fallback_model: Optional[str] = "gpt-4.1-nano",
        max_tokens: int = 2000,
    ):
        """Initialize OpenAI rewriter.

        Args:
            api_key: OpenAI API key.
            model: Primary model to use.
            fallback_model: Fallback model when primary call fails.
            max_tokens: Maximum tokens in response.
        """
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.fallback_model = fallback_model
        self.max_tokens = max_tokens
        self._last_request_time = 0.0

    def _rate_limit(self) -> None:
        """Ensure we don't exceed rate limits."""
        min_interval = 2.0  # 2 seconds between requests
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_time = time.time()

    def _uses_max_completion_tokens(self, model: str) -> bool:
        """Return True for models that require max_completion_tokens."""
        lower = model.lower()
        return lower.startswith("gpt-5") or any(
            token in lower for token in ["4.1", "4o", "o1", "o3", "o4"]
        )

    def _supports_temperature(self, model: str) -> bool:
        """Return True when model supports custom temperature values."""
        return not model.lower().startswith("gpt-5")

    def _supports_response_format(self, model: str) -> bool:
        """Return True when json_object response_format is supported."""
        return "o1" not in model.lower()

    def _build_api_params(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
    ) -> dict:
        """Build model-compatible chat completion parameters."""
        api_params = {
            "model": model,
            "messages": messages,
        }

        if self._uses_max_completion_tokens(model):
            api_params["max_completion_tokens"] = self.max_tokens
        else:
            api_params["max_tokens"] = self.max_tokens

        if self._supports_response_format(model):
            api_params["response_format"] = {"type": "json_object"}

        if self._supports_temperature(model):
            api_params["temperature"] = temperature

        return api_params

    def _models_to_try(self) -> list[str]:
        """Return primary model then fallback model (if configured)."""
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        return models

    def _request_json_completion(
        self,
        task: str,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
    ) -> Optional[dict]:
        """Call OpenAI and parse JSON response, with fallback model retry."""
        last_error = ""

        for idx, model in enumerate(self._models_to_try()):
            try:
                api_params = self._build_api_params(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                )

                response = self.client.chat.completions.create(**api_params)
                response_text = response.choices[0].message.content or ""
                parsed = self._parse_response(response_text)

                if parsed:
                    if idx == 0:
                        logger.info(
                            "openai_task_complete",
                            task=task,
                            model=model,
                            headline=parsed.get("headline", "")[:50],
                        )
                    else:
                        logger.info(
                            "fallback_openai_task_complete",
                            task=task,
                            model=model,
                            headline=parsed.get("headline", "")[:50],
                        )
                    return parsed

                last_error = "invalid_json_response"
                logger.warning("openai_invalid_json_response", task=task, model=model)

            except Exception as e:
                last_error = str(e)
                logger.error(
                    "openai_task_error",
                    task=task,
                    model=model,
                    error=str(e),
                )

            if idx < len(self._models_to_try()) - 1:
                logger.info(
                    "trying_fallback_model",
                    failed_model=model,
                    fallback=self._models_to_try()[idx + 1],
                    task=task,
                )

        logger.error("openai_all_models_failed", task=task, error=last_error)
        return None

    def rewrite(
        self,
        content: str,
        original_title: str,
        use_original_title: bool = False,
    ) -> Optional[dict]:
        """Rewrite content into AP-style article.

        Args:
            content: Original article content/HTML.
            original_title: Original article title.
            use_original_title: If True, keep the original title.

        Returns:
            Dictionary with headline, excerpt, body or None on failure.
        """
        self._rate_limit()

        # Clean HTML from content for better processing
        clean_content = self._strip_html(content)

        if not clean_content or len(clean_content) < 50:
            logger.warning("content_too_short", length=len(clean_content))
            return None

        # Truncate very long content
        if len(clean_content) > 10000:
            clean_content = clean_content[:10000] + "..."

        logger.info(
            "rewriting_article",
            title=original_title[:50],
            content_length=len(clean_content),
            model=self.model,
        )

        user_prompt = f"""Rewrite the following article into AP style:

ORIGINAL TITLE: {original_title}

ORIGINAL CONTENT:
{clean_content}

Remember to respond with valid JSON containing headline, excerpt, and body."""

        result = self._request_json_completion(
            task="rewrite",
            messages=[
                {"role": "system", "content": AP_STYLE_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.7,
        )

        if not result:
            return None

        # Override headline if requested
        if use_original_title:
            result["headline"] = original_title

        logger.info(
            "rewrite_complete",
            headline=result["headline"][:50],
            body_length=len(result["body"]),
        )
        return result

    def write_weekly_column(
        self,
        column_name: str,
        column_type: str,
        current_date: str,
        context_items: list[dict[str, str]],
    ) -> Optional[dict]:
        """Generate a weekly columnist article."""
        self._rate_limit()

        context_lines = []
        for item in context_items:
            title = item.get("title", "").strip()
            source = item.get("source", "").strip()
            link = item.get("link", "").strip()
            if title:
                context_line = f"- {title}"
                if source:
                    context_line += f" ({source})"
                if link:
                    context_line += f" [{link}]"
                context_lines.append(context_line)

        context_block = "\n".join(context_lines) if context_lines else "- No context items available"
        style_brief = _column_style_brief(column_type)

        user_prompt = f"""Write this week's syndicated column.

COLUMN NAME: {column_name}
COLUMN TYPE: {column_type}
CURRENT DATE: {current_date}

STYLE BRIEF:
{style_brief}

CONTEXT HEADLINES (use as factual anchors):
{context_block}

The column must be original and publication-ready. Return valid JSON with headline, excerpt, body, and image_query."""

        result = self._request_json_completion(
            task="weekly_column",
            messages=[
                {"role": "system", "content": WEEKLY_COLUMN_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
        )

        if not result:
            return None

        if not result.get("image_query"):
            result["image_query"] = f"{column_type} columnist"

        return result

    def _parse_response(self, response_text: str) -> Optional[dict]:
        """Parse the JSON response from OpenAI.

        Args:
            response_text: Raw response text.

        Returns:
            Parsed dictionary or None.
        """
        try:
            data = json.loads(response_text)
            return self._normalize_response(data)

        except json.JSONDecodeError as e:
            logger.warning("json_parse_error", error=str(e), response=response_text[:200])

            # Try to extract from malformed response
            return self._extract_fallback(response_text)

    def _normalize_response(self, data: dict) -> Optional[dict]:
        """Normalize and validate JSON payload from model."""
        if not isinstance(data, dict):
            logger.warning("invalid_response_type", response_type=type(data).__name__)
            return None

        headline = data.get("headline")
        body = data.get("body")
        if not isinstance(headline, str) or not isinstance(body, str):
            logger.warning("missing_required_fields")
            return None

        result = {
            "headline": headline.strip(),
            "excerpt": str(data.get("excerpt", "")).strip(),
            "body": body.strip(),
        }

        image_query = data.get("image_query")
        if isinstance(image_query, str) and image_query.strip():
            result["image_query"] = image_query.strip()

        tags = data.get("tags")
        if isinstance(tags, list):
            cleaned_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
            if cleaned_tags:
                result["tags"] = cleaned_tags

        return result

    def _extract_fallback(self, text: str) -> Optional[dict]:
        """Try to extract content from malformed response.

        Args:
            text: Response text that failed JSON parsing.

        Returns:
            Extracted dictionary or None.
        """
        try:
            # Try to find JSON-like content
            json_match = re.search(r"\{[\s\S]*\}", text)
            if json_match:
                extracted = json.loads(json_match.group())
                return self._normalize_response(extracted)
        except Exception:
            pass

        logger.warning("fallback_extraction_failed")
        return None

    def _strip_html(self, html: str) -> str:
        """Remove HTML tags and clean up content.

        Args:
            html: HTML content.

        Returns:
            Plain text content.
        """
        from bs4 import BeautifulSoup

        try:
            soup = BeautifulSoup(html, "html.parser")

            # Remove script and style elements
            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            # Get text
            text = soup.get_text(separator=" ")

            # Clean up whitespace
            text = re.sub(r"\s+", " ", text)
            text = text.strip()

            return text

        except Exception:
            # Fallback: simple regex
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            return text.strip()


def rewrite_with_openai(
    content: str,
    original_title: str,
    api_key: str,
    model: str = "gpt-5-mini",
    fallback_model: Optional[str] = "gpt-4.1-nano",
    use_original_title: bool = False,
) -> Optional[dict]:
    """Convenience function to rewrite content.

    Args:
        content: Original article content.
        original_title: Original title.
        api_key: OpenAI API key.
        model: Primary model to use.
        fallback_model: Fallback model.
        use_original_title: Keep original title if True.

    Returns:
        Dictionary with headline, excerpt, body or None.
    """
    rewriter = OpenAIRewriter(
        api_key=api_key,
        model=model,
        fallback_model=fallback_model,
    )
    return rewriter.rewrite(content, original_title, use_original_title)
