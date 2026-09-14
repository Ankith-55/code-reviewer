import os
import re
import json
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
import httpx

from core.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert static analysis and code review AI.
Review the following source code for:
- Bugs and logical errors
- Security vulnerabilities (SQL injection, XSS, insecure deserialization, command injection, secret leakage)
- Performance bottlenecks
- Code smells and bad practices
- Maintainability issues

Return ONLY a valid JSON object matching this exact structure:
{
    "issues": [
        {
            "line": 1,
            "severity": "HIGH",
            "category": "security",
            "description": "Short explanation of the issue",
            "recommendation": "Actionable fix"
        }
    ]
}

Allowed severity values: CRITICAL, HIGH, MEDIUM, LOW
Allowed categories: bug, security, performance, maintainability, style
If no issues are found, return {"issues": []}. Do not wrap in markdown tags. Return raw JSON.
"""


class BaseLLMService(ABC):
    @abstractmethod
    def analyze_code(self, code: str, language: str, file_path: str) -> List[Dict[str, Any]]:
        """Analyzes source code and returns a list of issue findings."""
        pass

    @staticmethod
    def clean_and_parse_json(raw_text: str) -> List[Dict[str, Any]]:
        """Cleans and extracts structured JSON issues list, handling markdown code fences."""
        cleaned = raw_text.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\n?", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\n?```$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback regex search for embedded JSON object
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(0))
                except Exception:
                    logger.warning(f"Failed to parse LLM JSON: {raw_text[:200]}")
                    return []
            else:
                logger.warning(f"No valid JSON object found in response: {raw_text[:200]}")
                return []

        raw_issues = data.get("issues", [])
        valid_issues = []
        valid_severities = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        valid_categories = {"bug", "security", "performance", "maintainability", "style"}

        for item in raw_issues:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "MEDIUM")).upper()
            if severity not in valid_severities:
                severity = "MEDIUM"

            category = str(item.get("category", "bug")).lower()
            if category not in valid_categories:
                category = "bug"

            line = item.get("line")
            if line is not None:
                try:
                    line = int(line)
                except (ValueError, TypeError):
                    line = None

            valid_issues.append({
                "line": line,
                "severity": severity,
                "category": category,
                "description": str(item.get("description", "Code issue detected")),
                "recommendation": str(item.get("recommendation", "Review and refactor")),
            })

        return valid_issues


class MockLLMService(BaseLLMService):
    """Deterministic, high-fidelity mock LLM service.

    Scans code for real-world anti-patterns and generates genuine findings.
    Allows testing and benchmarking without third-party API quotas or network flakiness.
    """

    def __init__(self, simulated_delay_ms: int = 50):
        self.simulated_delay_ms = simulated_delay_ms

    def analyze_code(self, code: str, language: str, file_path: str) -> List[Dict[str, Any]]:
        if self.simulated_delay_ms > 0:
            time.sleep(self.simulated_delay_ms / 1000.0)

        issues = []
        lines = code.splitlines()

        for idx, line in enumerate(lines, 1):
            line_str = line.strip()

            # Security: eval or exec
            if re.search(r"\b(eval|exec)\s*\(", line_str):
                issues.append({
                    "line": idx,
                    "severity": "CRITICAL",
                    "category": "security",
                    "description": "Dynamic code execution via eval/exec allows arbitrary code execution",
                    "recommendation": "Avoid eval/exec; use safe parsing libraries (e.g. ast.literal_eval)",
                })

            # Security: hardcoded secrets
            if re.search(r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]{6,}['\"]", line_str, re.IGNORECASE):
                issues.append({
                    "line": idx,
                    "severity": "HIGH",
                    "category": "security",
                    "description": "Hardcoded credential or secret detected in source code",
                    "recommendation": "Extract credentials into environment variables or secrets manager",
                })

            # Security: SQL injection (raw query formatting)
            if re.search(r"(SELECT|INSERT|UPDATE|DELETE)\s+.*(%s|\{\}|\+)|\bquery\s*=\s*f['\"].*\{", line_str, re.IGNORECASE):
                issues.append({
                    "line": idx,
                    "severity": "HIGH",
                    "category": "security",
                    "description": "Possible SQL injection through string concatenation or f-string formatting",
                    "recommendation": "Use parameterized queries or ORM bind parameters",
                })

            # Bug: Broad exception catching
            if re.search(r"except\s*:\s*$", line_str) or re.search(r"catch\s*\(\s*Exception\b", line_str):
                issues.append({
                    "line": idx,
                    "severity": "MEDIUM",
                    "category": "bug",
                    "description": "Catching broad/bare exceptions masks critical system errors and interrupts",
                    "recommendation": "Catch specific exception types rather than bare Exception",
                })

            # Performance: unindexed or costly pattern
            if "SELECT *" in line_str.upper():
                issues.append({
                    "line": idx,
                    "severity": "LOW",
                    "category": "performance",
                    "description": "Wildcard 'SELECT *' queries retrieve unnecessary columns over the wire",
                    "recommendation": "Explicitly specify only required column names in queries",
                })

            # Maintainability: TODO/FIXME comments
            if re.search(r"#\s*(TODO|FIXME|HACK)|//\s*(TODO|FIXME|HACK)", line_str, re.IGNORECASE):
                issues.append({
                    "line": idx,
                    "severity": "LOW",
                    "category": "maintainability",
                    "description": "Unresolved TODO/FIXME comment indicating incomplete implementation",
                    "recommendation": "Resolve technical debt or track in issue tracker",
                })

        # File-level heuristic: Large file warning
        if len(lines) > 300:
            issues.append({
                "line": 1,
                "severity": "MEDIUM",
                "category": "maintainability",
                "description": f"Source file has {len(lines)} lines, exceeding recommended module size",
                "recommendation": "Break down large file into smaller, cohesive modules",
            })

        return issues


class GeminiLLMService(BaseLLMService):
    """Google Gemini LLM provider via REST API."""

    def __init__(self, api_key: str = settings.LLM_API_KEY, model: str = settings.LLM_MODEL):
        self.api_key = api_key
        self.model = model or "gemini-1.5-flash"
        self.endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )

    def analyze_code(self, code: str, language: str, file_path: str) -> List[Dict[str, Any]]:
        if not self.api_key:
            logger.warning("LLM_API_KEY not set. Falling back to MockLLMService.")
            return MockLLMService().analyze_code(code, language, file_path)

        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"File: {file_path}\n"
            f"Language: {language}\n\n"
            f"Source Code:\n```{language}\n{code}\n```"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "response_mime_type": "application/json",
            },
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(self.endpoint, json=payload)
            response.raise_for_status()
            res_data = response.json()
            candidates = res_data.get("candidates", [])
            if not candidates:
                return []
            content_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
            return self.clean_and_parse_json(content_text)


def get_llm_service() -> BaseLLMService:
    """Factory creating configured LLM service based on environment settings."""
    provider = settings.LLM_PROVIDER.lower()
    if provider == "gemini" and settings.LLM_API_KEY:
        return GeminiLLMService()
    return MockLLMService()
