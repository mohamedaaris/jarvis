from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
import uuid
from typing import Literal
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)


class BrowserError(Exception):
    """A user-facing browser operation failure."""


def _focus_window_with_title(title: str, *, timeout_seconds: float = 2.0) -> bool:
    if sys.platform != "win32":
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_windows_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    user32.EnumWindows.argtypes = [enum_windows_proc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    def find_window() -> int | None:
        matches: list[int] = []

        @enum_windows_proc
        def collect_window(hwnd: int, _: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title in buffer.value:
                matches.append(hwnd)
                return False
            return True

        user32.EnumWindows(collect_window, 0)
        return matches[0] if matches else None

    deadline = time.monotonic() + timeout_seconds
    hwnd = find_window()
    while hwnd is None and time.monotonic() < deadline:
        time.sleep(0.05)
        hwnd = find_window()
    if hwnd is None:
        return False

    foreground = user32.GetForegroundWindow()
    current_thread = kernel32.GetCurrentThreadId()
    foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
    attached = bool(
        foreground_thread
        and foreground_thread != current_thread
        and user32.AttachThreadInput(current_thread, foreground_thread, True)
    )
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            0x0001 | 0x0002 | 0x0040,  # NOSIZE | NOMOVE | SHOWWINDOW
        )
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


async def _bring_page_window_to_front(page: Page) -> None:
    marker = f"Jarvis Browser {uuid.uuid4().hex}"
    original_title = ""
    title_changed = False
    try:
        original_title = await page.title()
        await page.evaluate("title => { document.title = title; }", marker)
        title_changed = True
        await page.bring_to_front()
        await asyncio.to_thread(_focus_window_with_title, marker)
    except Exception:
        # Foreground activation is best-effort and must not prevent browser use.
        return
    finally:
        if title_changed:
            with contextlib.suppress(Exception):
                await page.evaluate(
                    "title => { document.title = title; }",
                    original_title,
                )


class BrowserManager:
    """Own one isolated, visible browser for a LiveKit room with multi-tab support."""

    def __init__(
        self, *, headless: bool = False, timeout_ms: int = 15_000, max_pages: int = 5
    ) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._max_pages = max_pages
        self._playwright = None
        self._browser = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}
        self._active_page_id: str | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._playwright is not None:
            return

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
        )
        # Create default context and page
        await self._create_default_context_and_page()

    async def _create_default_context_and_page(self) -> None:
        context_id = "default"
        page_id = "default"

        context = await self._browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(self._timeout_ms)

        self._contexts[context_id] = context
        self._pages[page_id] = page
        self._active_page_id = page_id

        if not self._headless:
            await _bring_page_window_to_front(page)

    async def close(self) -> None:
        async with self._lock:
            for context in self._contexts.values():
                await context.close()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()

            self._pages.clear()
            self._contexts.clear()
            self._active_page_id = None
            self._browser = None
            self._playwright = None

    # ===== Page/Context Management (Phase 1) =====

    async def new_page(
        self, *, context_id: str = "default", page_id: str | None = None
    ) -> dict[str, str]:
        """Create a new page in the specified context."""
        if len(self._pages) >= self._max_pages:
            raise BrowserError(
                f"Maximum number of pages ({self._max_pages}) reached. Close a page first."
            )

        async with self._lock:
            context = self._contexts.get(context_id)
            if context is None:
                context = await self._browser.new_context()
                self._contexts[context_id] = context

            if page_id is None:
                page_id = f"page_{len(self._pages) + 1}"
            elif page_id in self._pages:
                raise BrowserError(f"Page with id '{page_id}' already exists.")

            page = await context.new_page()
            page.set_default_timeout(self._timeout_ms)
            self._pages[page_id] = page
            self._active_page_id = page_id

            if not self._headless:
                await _bring_page_window_to_front(page)

            return {"page_id": page_id, "context_id": context_id, "url": page.url}

    async def close_page(self, page_id: str) -> dict[str, str]:
        """Close a specific page."""
        async with self._lock:
            page = self._pages.pop(page_id, None)
            if page is None:
                raise BrowserError(f"No page found with id: {page_id}")

            await page.close()

            # If we closed the active page, switch to another
            if self._active_page_id == page_id:
                self._active_page_id = next(iter(self._pages), None)

            return {"closed": page_id, "active_page": self._active_page_id}

    async def switch_page(self, page_id: str) -> dict[str, str]:
        """Switch the active page."""
        async with self._lock:
            if page_id not in self._pages:
                raise BrowserError(f"No page found with id: {page_id}")
            self._active_page_id = page_id
            page = self._pages[page_id]
            if not self._headless:
                await _bring_page_window_to_front(page)
            return {"active_page": page_id, "url": page.url}

    async def list_pages(self) -> dict[str, object]:
        """List all open pages with their info."""
        async with self._lock:
            pages_info = []
            for pid, page in self._pages.items():
                pages_info.append(
                    {
                        "page_id": pid,
                        "url": page.url,
                        "title": await page.title(),
                        "active": pid == self._active_page_id,
                    }
                )
            return {"pages": pages_info, "active_page": self._active_page_id}

    async def new_context(self, context_id: str) -> dict[str, str]:
        """Create a new isolated browser context (separate cookies/storage)."""
        async with self._lock:
            if context_id in self._contexts:
                raise BrowserError(f"Context with id '{context_id}' already exists.")
            context = await self._browser.new_context()
            self._contexts[context_id] = context
            return {"context_id": context_id}

    async def close_context(self, context_id: str) -> dict[str, str]:
        """Close a browser context and all its pages."""
        async with self._lock:
            context = self._contexts.pop(context_id, None)
            if context is None:
                raise BrowserError(f"No context found with id: {context_id}")

            # Close all pages in this context
            pages_to_close = [
                pid for pid, page in self._pages.items() if page.context == context
            ]
            for pid in pages_to_close:
                await self._pages[pid].close()
                del self._pages[pid]

            await context.close()

            if self._active_page_id in pages_to_close:
                self._active_page_id = next(iter(self._pages), None)

            return {"closed_context": context_id, "closed_pages": pages_to_close}

    # ===== Core Navigation & Content (existing, adapted) =====

    async def _get_page(self, page_id: str | None = None) -> Page:
        # Auto-start browser if not started
        if self._playwright is None:
            await self.start()

        if page_id is None:
            page_id = self._active_page_id or "default"

        page = self._pages.get(page_id)
        if page is None:
            raise BrowserError(f"No page found with id: {page_id}")
        return page

    async def _get_context(self, context_id: str | None = None) -> BrowserContext:
        if context_id is None:
            page_id = self._active_page_id or "default"
            page = self._pages.get(page_id)
            if page:
                return page.context
            context_id = "default"

        context = self._contexts.get(context_id)
        if context is None:
            raise BrowserError(f"No context found with id: {context_id}")
        return context

    async def open_url(self, url: str, *, page_id: str | None = None) -> dict[str, str]:
        self._validate_url(url)
        page = await self._get_page(page_id)

        async with self._lock:
            try:
                await page.goto(url, wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page took too long to load.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not open that page: {exc}") from exc

    async def read_page(
        self, *, max_chars: int = 12_000, page_id: str | None = None
    ) -> dict[str, str | bool]:
        page = await self._get_page(page_id)

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    "The page content was not available in time."
                ) from exc

            text = re.sub(r"\s+", " ", text).strip()
            truncated = len(text) > max_chars
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "truncated": truncated,
            }

    async def inspect_page(
        self, *, max_chars: int = 8_000, page_id: str | None = None
    ) -> dict[str, object]:
        """Return readable text plus a compact inventory of interactive elements."""
        page = await self._get_page(page_id)

        async with self._lock:
            try:
                text = await page.locator("body").inner_text(timeout=self._timeout_ms)
                elements = await page.locator(
                    "button, a, input, textarea, select, [role]"
                ).evaluate_all(
                    """elements => elements
                      .filter(element => {
                        const style = window.getComputedStyle(element);
                        return style.display !== 'none' && style.visibility !== 'hidden';
                      })
                      .slice(0, 80)
                      .map((element, index) => ({
                        index,
                        tag: element.tagName.toLowerCase(),
                        role: element.getAttribute('role') || '',
                        name: (element.getAttribute('aria-label') ||
                          element.getAttribute('title') ||
                          (element.labels && element.labels[0] && element.labels[0].innerText) ||
                          element.getAttribute('placeholder') ||
                          element.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
                        type: element.getAttribute('type') || '',
                      }))"""
                )
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The page could not be inspected in time.") from exc

            text = re.sub(r"\s+", " ", text).strip()
            return {
                "url": page.url,
                "title": await page.title(),
                "text": text[:max_chars],
                "elements": elements,
            }

    async def go_back(self, *, page_id: str | None = None) -> dict[str, str]:
        page = await self._get_page(page_id)

        async with self._lock:
            try:
                await page.go_back(wait_until="domcontentloaded")
                return await self._page_summary(page)
            except PlaywrightTimeoutError as exc:
                raise BrowserError("The previous page took too long to load.") from exc
            except Exception as exc:
                raise BrowserError(f"I could not go back: {exc}") from exc

    async def take_screenshot(
        self, *, page_id: str | None = None, full_page: bool = False
    ) -> dict[str, str | int | bool]:
        page = await self._get_page(page_id)

        async with self._lock:
            try:
                image = await page.screenshot(type="png", full_page=full_page)
                return {
                    "captured": True,
                    "url": page.url,
                    "bytes": len(image),
                    "full_page": full_page,
                }
            except Exception as exc:
                raise BrowserError(f"I could not capture the page: {exc}") from exc

    # ===== Interaction Methods (existing + Phase 2 additions) =====

    async def click(self, target: str, *, page_id: str | None = None) -> dict[str, str]:
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.click()
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    f"The control {target!r} did not become clickable in time."
                ) from exc
            except Exception as exc:
                raise BrowserError(f"I could not click {target!r}: {exc}") from exc

            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)

            return await self._page_summary(page)

    async def type_text(
        self, target: str, text: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_textbox(page, target)
            try:
                await locator.fill(text)
            except Exception as exc:
                raise BrowserError(f"I could not type into {target!r}: {exc}") from exc

            return {"target": target, "url": page.url}

    # --- Phase 2: Form Controls ---

    async def select_option(
        self, target: str, value: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Select an option in a dropdown/select element by value or label."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.select_option(value=value)
            except Exception as exc:
                raise BrowserError(
                    f"I could not select option {value!r} in {target!r}: {exc}"
                ) from exc

            return {"target": target, "value": value, "url": page.url}

    async def check(self, target: str, *, page_id: str | None = None) -> dict[str, str]:
        """Check a checkbox or radio button."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.check()
            except Exception as exc:
                raise BrowserError(f"I could not check {target!r}: {exc}") from exc

            return {"target": target, "checked": True, "url": page.url}

    async def uncheck(
        self, target: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Uncheck a checkbox."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.uncheck()
            except Exception as exc:
                raise BrowserError(f"I could not uncheck {target!r}: {exc}") from exc

            return {"target": target, "checked": False, "url": page.url}

    async def hover(self, target: str, *, page_id: str | None = None) -> dict[str, str]:
        """Hover over an element."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.hover()
            except Exception as exc:
                raise BrowserError(f"I could not hover over {target!r}: {exc}") from exc

            return {"target": target, "url": page.url}

    async def double_click(
        self, target: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Double-click an element."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.dblclick()
            except Exception as exc:
                raise BrowserError(
                    f"I could not double-click {target!r}: {exc}"
                ) from exc

            with contextlib.suppress(PlaywrightTimeoutError):
                await page.wait_for_load_state("domcontentloaded", timeout=5_000)

            return await self._page_summary(page)

    async def right_click(
        self, target: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Right-click an element (context menu)."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.click(button="right")
            except Exception as exc:
                raise BrowserError(
                    f"I could not right-click {target!r}: {exc}"
                ) from exc

            return {"target": target, "url": page.url}

    async def focus_element(
        self, target: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Focus an element (for keyboard navigation)."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.focus()
            except Exception as exc:
                raise BrowserError(f"I could not focus {target!r}: {exc}") from exc

            return {"target": target, "url": page.url}

    async def upload_file(
        self, target: str, file_path: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Upload a file to a file input element."""
        page = await self._get_page(page_id)

        async with self._lock:
            locator = await self._resolve_target(page, target)
            try:
                await locator.set_input_files(file_path)
            except Exception as exc:
                raise BrowserError(
                    f"I could not upload file to {target!r}: {exc}"
                ) from exc

            return {"target": target, "file_path": file_path, "url": page.url}

    async def drag_and_drop(
        self, source: str, target: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        """Drag an element and drop it on another element."""
        page = await self._get_page(page_id)

        async with self._lock:
            source_locator = await self._resolve_target(page, source)
            target_locator = await self._resolve_target(page, target)
            try:
                await source_locator.drag_to(target_locator)
            except Exception as exc:
                raise BrowserError(
                    f"I could not drag {source!r} to {target!r}: {exc}"
                ) from exc

            return {"source": source, "target": target, "url": page.url}

    async def scroll(
        self, direction: Literal["up", "down"], *, page_id: str | None = None
    ) -> dict[str, str]:
        if direction not in {"up", "down"}:
            raise BrowserError("Scroll direction must be 'up' or 'down'.")

        page = await self._get_page(page_id)
        amount = -650 if direction == "up" else 650

        async with self._lock:
            await page.mouse.wheel(0, amount)
            return {"direction": direction, "url": page.url}

    async def press_key(
        self, key: str, *, page_id: str | None = None
    ) -> dict[str, str]:
        allowed_keys = {
            "Enter",
            "Escape",
            "Tab",
            "ArrowDown",
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "Backspace",
        }
        if key not in allowed_keys:
            raise BrowserError("That keyboard key is not allowed.")

        page = await self._get_page(page_id)
        async with self._lock:
            await page.keyboard.press(key)
            return {"key": key, "url": page.url}

    # ===== Helper Methods =====

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrowserError("Only complete http or https URLs can be opened.")
        if parsed.username or parsed.password:
            raise BrowserError("URLs containing embedded credentials are not allowed.")

    @staticmethod
    async def _page_summary(page: Page) -> dict[str, str]:
        return {"url": page.url, "title": await page.title()}

    async def _resolve_target(self, page: Page, target: str) -> Locator:
        for role in ("button", "link", "tab", "menuitem", "checkbox", "radio"):
            locator = page.get_by_role(role, name=target, exact=True)
            if await locator.count():
                return locator.first

            locator = page.get_by_role(role, name=target, exact=False)
            if await locator.count():
                return locator.first

        locator = page.get_by_text(target, exact=True)
        if await locator.count():
            return locator.first

        locator = page.get_by_text(target, exact=False)
        if await locator.count():
            return locator.first

        raise BrowserError(f"I could not find a visible control named {target!r}.")

    async def _resolve_textbox(self, page: Page, target: str) -> Locator:
        for locator in (
            page.get_by_role("textbox", name=target, exact=True),
            page.get_by_role("textbox", name=target, exact=False),
            page.get_by_label(target, exact=True),
            page.get_by_label(target, exact=False),
            page.get_by_placeholder(target, exact=True),
            page.get_by_placeholder(target, exact=False),
        ):
            if await locator.count():
                return locator.first

        normalized_target = target.casefold()
        if any(
            word in normalized_target for word in ("search", "query", "input", "text")
        ):
            for selector in (
                "input[type='search']:visible",
                "input[aria-label*='search' i]:visible",
                "input[placeholder*='search' i]:visible",
                "textarea:visible",
                "input:visible",
            ):
                locator = page.locator(selector)
                if await locator.count():
                    return locator.first

        raise BrowserError(f"I could not find a text field named {target!r}.")
