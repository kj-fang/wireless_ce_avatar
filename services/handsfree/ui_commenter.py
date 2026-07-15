"""
Selenium fallback poster — drives the IPS (Lightning) "Add Comment" UI when
REST posting is denied or unverified.

Extends the click/type/wait pattern proven in
CaseService._download_pdf_by_simulation. Lightning DOM is org-specific, so
every locator is overridable via the orchestrator config key "ui_locators";
the defaults below match the described flow (Add Comment tab → Private to
Intel option → body → Save) and get tuned once against the real page.

On any failure a screenshot + page source snippet is saved under
<avatarfiles_dir>/handsfree/ui_failures/ for locator debugging.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .ips_client import PostResult

DEFAULT_LOCATORS = {
    "case_url": "https://intel.lightning.force.com/lightning/r/Case/{case_id}/view",
    # The Add Comment tab/button on the case page.
    "add_comment_tab": "//a[.//span[normalize-space()='Add Comment']] | //button[normalize-space()='Add Comment'] | //li[@title='Add Comment']//a",
    # 'Private to Intel' choice — checkbox or picklist option.
    "private_option": "//label[contains(normalize-space(), 'Private to Intel')] | //span[normalize-space()='Private to Intel']",
    # Comment body: rich-text iframe OR plain textarea.
    "body_textarea": "//textarea | //div[@contenteditable='true']",
    "save_button": "//button[normalize-space()='Save'] | //button[normalize-space()='Add Comment'] | //button[.//span[normalize-space()='Save']]",
    "wait_secs": 45,
}


class UiCommenter:
    def __init__(self, locators: Optional[dict] = None):
        self.locators = {**DEFAULT_LOCATORS, **(locators or {})}

    def _failure_dir(self) -> Path:
        from configs.global_configs import app_config
        d = Path(app_config.avatarfiles_dir) / "handsfree" / "ui_failures"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _snapshot_failure(self, driver, tag: str) -> str:
        try:
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            base = self._failure_dir() / f"{stamp}_{tag}"
            driver.save_screenshot(str(base) + ".png")
            (Path(str(base) + ".html")).write_text(
                driver.page_source[:200_000], encoding="utf-8")
            return str(base) + ".png"
        except Exception:
            return ""

    def post_comment(self, case_id: str, body_text: str) -> PostResult:
        """Open the case page, add a Private-to-Intel comment, save."""
        from configs.global_configs import app_config
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        loc = self.locators
        wait_secs = int(loc.get("wait_secs", 45))
        driver_manager = app_config.driver_manager
        tmp_dir = os.path.join(app_config.avatarfiles_dir, "_handsfree_ui_tmp")
        driver = driver_manager.create_download_driver(tmp_dir)
        try:
            url = loc["case_url"].format(case_id=case_id)
            driver.get(url)
            wait = WebDriverWait(driver, wait_secs)
            # SSO redirect settles when we're on a force.com page (mirrors
            # _get_vf_session's condition).
            wait.until(lambda d: "force.com" in d.current_url
                       and "login" not in d.current_url)

            tab = wait.until(EC.element_to_be_clickable(
                (By.XPATH, loc["add_comment_tab"])))
            tab.click()
            time.sleep(1.5)

            # Private to Intel — click the labeled option if present; some
            # layouts default to private, so a missing locator is non-fatal
            # ONLY when configured as optional.
            try:
                private = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.XPATH, loc["private_option"])))
                private.click()
            except Exception:
                snap = self._snapshot_failure(driver, "private_option")
                return PostResult(
                    ok=False, backend="ui",
                    error="could not locate the 'Private to Intel' option — "
                          f"refusing to post a possibly-public comment (screenshot: {snap})")

            body = wait.until(EC.presence_of_element_located(
                (By.XPATH, loc["body_textarea"])))
            body.click()
            body.send_keys(body_text)

            save = wait.until(EC.element_to_be_clickable(
                (By.XPATH, loc["save_button"])))
            save.click()
            # Give Lightning a moment to persist; failure toast would remain.
            time.sleep(3)
            return PostResult(ok=True, backend="ui")
        except Exception as e:
            snap = self._snapshot_failure(driver, "post_comment")
            return PostResult(ok=False, backend="ui",
                              error=f"{type(e).__name__}: {e} (screenshot: {snap})")
        finally:
            try:
                driver.quit()
                if driver in driver_manager.all_drivers:
                    driver_manager.all_drivers.remove(driver)
            except Exception:
                pass
