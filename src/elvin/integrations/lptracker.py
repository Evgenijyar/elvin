"""Async client for the official LPTracker Direct API."""

import logging
from time import perf_counter
from typing import Any

import httpx


logger = logging.getLogger("elvin.lptracker")


class LPTrackerError(RuntimeError):
    """Structured LPTracker API error."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        api_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.api_code = api_code


class LPTrackerClient:
    """Small explicit LPTracker client mirroring the proven Java flow."""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def login(self, login: str, password: str) -> str:
        payload = {
            "login": login,
            "password": password,
            "service": "Elvin",
            "version": "1.0",
        }
        response = await self.client.post("/login", json=payload)
        data = self._decode(response)
        result = self._unwrap(data, response.status_code)
        token = result.get("token") if isinstance(result, dict) else None
        if not token:
            raise LPTrackerError("LPTracker не вернул токен авторизации.")
        return str(token)

    async def projects(self, token: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            "/projects",
            headers={"token": token},
        )
        result = self._unwrap(self._decode(response), response.status_code)
        if not isinstance(result, list):
            return []
        return [
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or f"Проект {item['id']}"),
                "page": item.get("page"),
                "domain": item.get("domain"),
            }
            for item in result
            if isinstance(item, dict) and item.get("id") is not None
        ]

    async def stages(
        self,
        token: str,
        project_id: int,
    ) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/project/{project_id}/funnel",
            headers={"token": token},
        )
        result = self._unwrap(self._decode(response), response.status_code)
        if not isinstance(result, list):
            return []
        return [
            {
                "id": int(item["id"]),
                "name": str(item.get("name") or f"Стадия {item['id']}"),
            }
            for item in result
            if isinstance(item, dict) and item.get("id") is not None
        ]

    async def register_lead_webhook(
        self,
        token: str,
        project_id: int,
        callback_url: str,
    ) -> bool:
        response = await self.client.put(
            f"/project/{project_id}/callback-url",
            headers={"token": token},
            json={"url": callback_url, "name": "Elvin"},
        )
        self._unwrap(self._decode(response), response.status_code)
        return True

    async def lead_preview(
        self,
        token: str,
        project_id: int,
        stage_id: int,
        *,
        max_scan: int = 300,
    ) -> dict[str, Any]:
        leads, stats = await self._collect_stage_leads(
            token,
            project_id,
            stage_id,
            max_scan=max_scan,
            max_results=max_scan,
        )
        return {
            **stats,
            "matched_count": len(leads),
            "items": leads[:20],
        }

    async def leads_for_stage(
        self,
        token: str,
        project_id: int,
        stage_id: int,
        *,
        max_results: int = 50,
        max_scan: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return ordered leads used to build a persistent call queue."""
        leads, _stats = await self._collect_stage_leads(
            token,
            project_id,
            stage_id,
            max_scan=max_scan,
            max_results=max_results,
        )
        return leads

    async def _collect_stage_leads(
        self,
        token: str,
        project_id: int,
        stage_id: int,
        *,
        max_scan: int,
        max_results: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        matched: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        scanned = 0
        with_phone = 0
        offset = 0
        page_size = 100

        while scanned < max_scan and len(matched) < max_results:
            response = await self.client.get(
                f"/lead/{project_id}/list",
                headers={"token": token},
                params={
                    "offset": offset,
                    "limit": page_size,
                    "sort[updated_at]": 3,
                    "is_deal": "false",
                },
            )
            data = self._decode(response)
            if response.status_code < 200 or response.status_code >= 300:
                self._unwrap(data, response.status_code)
            leads = self._extract_leads(data)
            if not leads:
                break

            for lead in leads:
                scanned += 1
                lead_id_value = lead.get("id")
                try:
                    lead_id = int(lead_id_value)
                except (TypeError, ValueError):
                    lead_id = 0
                actual_stage = self._extract_stage_id(lead)
                phone = self._extract_phone(lead)
                if phone:
                    with_phone += 1
                if (
                    lead_id
                    and lead_id not in seen_ids
                    and actual_stage == stage_id
                    and phone
                ):
                    seen_ids.add(lead_id)
                    contact = lead.get("contact") or {}
                    matched.append(
                        {
                            "lead_id": lead_id,
                            "lead_name": lead.get("name") or "Без названия",
                            "contact_name": contact.get("name") or "",
                            "phone": self._mask_phone(phone),
                            "stage_id": actual_stage,
                        }
                    )
                if scanned >= max_scan or len(matched) >= max_results:
                    break

            if len(leads) < page_size or scanned >= max_scan:
                break
            offset += len(leads)

        return matched, {
            "scanned_count": scanned,
            "with_phone_count": with_phone,
        }

    async def call_lead(
        self,
        token: str,
        lead_id: int,
    ) -> Any:
        started_at = perf_counter()
        response = await self.client.post(
            f"/lead/{lead_id}/call",
            headers={"token": token},
        )
        result = self._unwrap(
            self._decode(response),
            response.status_code,
        )
        logger.warning(
            "LPTracker /lead/%s/call accepted: http=%s elapsed=%.3fs",
            lead_id,
            response.status_code,
            perf_counter() - started_at,
        )
        return result

    def _decode(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise LPTrackerError(
                "LPTracker вернул ответ, который не является JSON.",
                http_status=response.status_code,
            ) from exc

    def _unwrap(self, data: Any, http_status: int) -> Any:
        if http_status < 200 or http_status >= 300:
            raise LPTrackerError(
                self._error_message(data) or f"LPTracker HTTP {http_status}",
                http_status=http_status,
            )
        if isinstance(data, dict) and data.get("status") == "error":
            errors = data.get("errors") or []
            first = errors[0] if errors and isinstance(errors[0], dict) else {}
            raise LPTrackerError(
                str(first.get("message") or "Ошибка LPTracker"),
                http_status=http_status,
                api_code=first.get("code"),
            )
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def _error_message(self, data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        errors = data.get("errors") or []
        if errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or "") or None
        return None

    def _extract_leads(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("result", "data", "items", "leads"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
                if isinstance(value, dict):
                    for nested in ("items", "data", "leads"):
                        nested_value = value.get(nested)
                        if isinstance(nested_value, list):
                            return [
                                item
                                for item in nested_value
                                if isinstance(item, dict)
                            ]
        return []

    def _extract_stage_id(self, lead: dict[str, Any]) -> int | None:
        stage_id = lead.get("stage_id")
        if stage_id is not None:
            try:
                return int(stage_id)
            except (TypeError, ValueError):
                pass
        for field in lead.get("custom") or []:
            if not isinstance(field, dict) or field.get("type") != "funnel":
                continue
            try:
                return int(field.get("value"))
            except (TypeError, ValueError):
                return None
        return None

    def _extract_phone(self, lead: dict[str, Any]) -> str | None:
        contact = lead.get("contact") or {}
        for detail in contact.get("details") or []:
            if not isinstance(detail, dict):
                continue
            detail_type = str(detail.get("type") or "").lower()
            if "phone" in detail_type:
                value = str(detail.get("data") or "").strip()
                if value:
                    return value
        return None

    def _mask_phone(self, phone: str) -> str:
        compact = "".join(character for character in phone if character.isdigit())
        if len(compact) <= 4:
            return "***"
        return f"+***{compact[-4:]}"
