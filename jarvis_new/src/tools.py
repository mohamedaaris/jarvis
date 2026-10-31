from urllib.parse import urlencode

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from browser import BrowserError, BrowserManager


def duckduckgo_search_url(query: str) -> str:
    query = query.strip()
    if not query:
        raise ValueError("The search query cannot be empty.")
    return f"https://duckduckgo.com/?{urlencode({'q': query})}"


class BrowserTools:
    def __init__(self, browser: BrowserManager) -> None:
        self.browser = browser
        self._confirmed_target: str | None = None

    @property
    def tools(self) -> list:
        return [
            self.open_url,
            self.search_the_web,
            self.read_page,
            self.inspect_page,
            self.go_back,
            self.take_screenshot,
            self.click,
            self.confirm_browser_action,
            self.type_text,
            self.scroll,
            self.press_key,
            # Phase 1: Multi-tab/Context Management
            self.new_page,
            self.close_page,
            self.switch_page,
            self.list_pages,
            self.new_context,
            self.close_context,
            # Phase 2: Form Controls & Advanced Interaction
            self.select_option,
            self.check,
            self.uncheck,
            self.hover,
            self.double_click,
            self.right_click,
            self.focus_element,
            self.upload_file,
            self.drag_and_drop,
        ]

    # ===== Existing Tools =====

    @function_tool()
    async def search_the_web(
        self,
        context: RunContext,
        query: str,
    ) -> dict[str, str]:
        """Open fallback DuckDuckGo results in the agent-controlled browser.

        Use this only when the user needs a general internet search and did not name a
        website, service, or domain. If the user names a destination, open its official
        URL directly with open_url instead. Read or inspect the resulting page before
        answering the user.

        Args:
            query: A concise DuckDuckGo search query containing all relevant context.
        """
        try:
            return await self.browser.open_url(duckduckgo_search_url(query))
        except (BrowserError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def open_url(
        self, context: RunContext, url: str, page_id: str | None = None
    ) -> dict[str, str]:
        """Open a public webpage directly in the agent-controlled browser.

        Prefer this over DuckDuckGo whenever the user names a website, service, domain,
        or specific destination. Use the destination's official URL.

        Args:
            url: A complete http or https URL to open.
            page_id: Optional page ID to open the URL in a specific tab.
        """
        try:
            return await self.browser.open_url(url, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def read_page(
        self, context: RunContext, page_id: str | None = None
    ) -> dict[str, str | bool]:
        """Read the visible text from the current browser page."""
        try:
            return await self.browser.read_page(page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def inspect_page(
        self, context: RunContext, page_id: str | None = None
    ) -> dict[str, object]:
        """Inspect the current page, including readable text and interactive element names.

        Use this before clicking or typing so you can choose a visible control by its
        returned name or role.
        """
        try:
            return await self.browser.inspect_page(page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def go_back(
        self, context: RunContext, page_id: str | None = None
    ) -> dict[str, str]:
        """Go back to the previous page in the agent-controlled browser."""
        try:
            return await self.browser.go_back(page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def take_screenshot(
        self, context: RunContext, page_id: str | None = None, full_page: bool = False
    ) -> dict[str, str | int | bool]:
        """Capture the current browser page for diagnostics.

        Args:
            page_id: Optional page ID to capture a specific tab.
            full_page: If true, capture the full scrollable page.
        """
        try:
            return await self.browser.take_screenshot(
                page_id=page_id, full_page=full_page
            )
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def click(
        self, context: RunContext, target: str, page_id: str | None = None
    ) -> dict[str, str]:
        """Click a visible control by its accessible name.

        Args:
            target: The visible or accessible name of the control to click.
            page_id: Optional page ID to click in a specific tab.
        """
        if self._requires_confirmation(target):
            if self._confirmed_target != target.casefold():
                raise ToolError(
                    f"This action may be consequential. Ask the user to confirm clicking {target!r} before retrying."
                )
            self._confirmed_target = None

        try:
            return await self.browser.click(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def confirm_browser_action(self, context: RunContext, target: str) -> str:
        """Authorize one previously discussed consequential browser click.

        Call this only after the user explicitly confirms the exact action.

        Args:
            target: The exact accessible name of the control the user approved.
        """
        self._confirmed_target = target.casefold()
        return f"The user confirmed clicking {target!r}."

    @function_tool()
    async def type_text(
        self,
        context: RunContext,
        target: str,
        text: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Fill a visible text field by its label, placeholder, or accessible name.

        Args:
            target: The label, placeholder, or accessible name of the text field.
            text: The text to enter.
            page_id: Optional page ID to type in a specific tab.
        """
        try:
            return await self.browser.type_text(target, text, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def scroll(
        self, context: RunContext, direction: str, page_id: str | None = None
    ) -> dict[str, str]:
        """Scroll the current browser page up or down.

        Args:
            direction: Either 'up' or 'down'.
            page_id: Optional page ID to scroll a specific tab.
        """
        try:
            return await self.browser.scroll(direction, page_id=page_id)  # type: ignore[arg-type]
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def press_key(
        self, context: RunContext, key: str, page_id: str | None = None
    ) -> dict[str, str]:
        """Press a safe navigation key in the current browser page.

        Args:
            key: One of Enter, Escape, Tab, an arrow key, or Backspace.
            page_id: Optional page ID to press key in a specific tab.
        """
        try:
            return await self.browser.press_key(key, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    # ===== Phase 1: Multi-tab/Context Management =====

    @function_tool()
    async def new_page(
        self,
        context: RunContext,
        context_id: str = "default",
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Create a new browser tab/page in the specified context.

        Args:
            context_id: The browser context to create the page in (default: "default").
            page_id: Optional custom ID for the new page. Auto-generated if not provided.
        """
        try:
            return await self.browser.new_page(context_id=context_id, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def close_page(self, context: RunContext, page_id: str) -> dict[str, str]:
        """Close a specific browser tab/page.

        Args:
            page_id: The ID of the page to close.
        """
        try:
            return await self.browser.close_page(page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def switch_page(self, context: RunContext, page_id: str) -> dict[str, str]:
        """Switch the active browser tab/page.

        Args:
            page_id: The ID of the page to make active.
        """
        try:
            return await self.browser.switch_page(page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def list_pages(self, context: RunContext) -> dict[str, object]:
        """List all open browser tabs/pages with their URLs and titles."""
        try:
            return await self.browser.list_pages()
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def new_context(self, context: RunContext, context_id: str) -> dict[str, str]:
        """Create a new isolated browser context (separate cookies, storage, cache).

        Args:
            context_id: Unique identifier for the new context.
        """
        try:
            return await self.browser.new_context(context_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def close_context(
        self, context: RunContext, context_id: str
    ) -> dict[str, str]:
        """Close a browser context and all its pages.

        Args:
            context_id: The ID of the context to close.
        """
        try:
            return await self.browser.close_context(context_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    # ===== Phase 2: Form Controls & Advanced Interaction =====

    @function_tool()
    async def select_option(
        self,
        context: RunContext,
        target: str,
        value: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Select an option in a dropdown/select element by its value.

        Args:
            target: The visible or accessible name of the select element.
            value: The value attribute of the option to select.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.select_option(target, value, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def check(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Check a checkbox or select a radio button.

        Args:
            target: The visible or accessible name of the checkbox/radio.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.check(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def uncheck(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Uncheck a checkbox.

        Args:
            target: The visible or accessible name of the checkbox.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.uncheck(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def hover(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Hover the mouse over an element (for dropdown menus, tooltips, etc.).

        Args:
            target: The visible or accessible name of the element to hover.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.hover(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def double_click(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Double-click an element.

        Args:
            target: The visible or accessible name of the element to double-click.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.double_click(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def right_click(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Right-click an element (opens context menu).

        Args:
            target: The visible or accessible name of the element to right-click.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.right_click(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def focus_element(
        self,
        context: RunContext,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Focus an element for keyboard navigation.

        Args:
            target: The visible or accessible name of the element to focus.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.focus_element(target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def upload_file(
        self,
        context: RunContext,
        target: str,
        file_path: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Upload a file to a file input element.

        Args:
            target: The visible or accessible name of the file input element.
            file_path: The local path to the file to upload.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.upload_file(target, file_path, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @function_tool()
    async def drag_and_drop(
        self,
        context: RunContext,
        source: str,
        target: str,
        page_id: str | None = None,
    ) -> dict[str, str]:
        """Drag an element and drop it on another element.

        Args:
            source: The visible or accessible name of the element to drag.
            target: The visible or accessible name of the drop target.
            page_id: Optional page ID to interact with a specific tab.
        """
        try:
            return await self.browser.drag_and_drop(source, target, page_id=page_id)
        except BrowserError as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _requires_confirmation(target: str) -> bool:
        risky_words = {
            "buy",
            "confirm",
            "delete",
            "purchase",
            "remove",
            "send",
            "submit",
        }
        return bool(risky_words.intersection(target.casefold().split()))
