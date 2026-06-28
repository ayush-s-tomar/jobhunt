"""
apply_bot.py
─────────────
Handles both apply paths after user confirms a job:

PATH A — Email:   Sends resume + cover letter via SMTP
PATH B — URL:     Uses Playwright to fill & submit the application form

Returns an ApplyResult with success status and log entries.
"""

import os, asyncio, logging, aiofiles
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SMTP_HOST  = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT  = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER  = os.getenv("SMTP_USER", "")
SMTP_PASS  = os.getenv("SMTP_PASS", "")
FROM_NAME  = os.getenv("EMAIL_FROM_NAME", "Applicant")


@dataclass
class ApplyResult:
    success: bool
    method: str                       # "email" | "form" | "captcha_blocked" | "manual"
    logs: list = field(default_factory=list)
    captcha_blocked: bool = False

    def log_step(self, msg: str):
        entry = {"ts": datetime.utcnow().isoformat(), "msg": msg}
        self.logs.append(entry)
        log.info(f"[apply_bot] {msg}")


# ── PATH A: Email apply ───────────────────────────────────────────────────────
async def apply_via_email(
    to_email: str,
    job_title: str,
    company: str,
    cover_letter: str,
    resume_path: str,
    applicant_name: str,
    from_email: str,
) -> ApplyResult:
    result = ApplyResult(success=False, method="email")
    result.log_step(f"Preparing email to {to_email}")

    try:
        import aiosmtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.application import MIMEApplication

        msg = MIMEMultipart()
        msg["From"]    = f"{FROM_NAME} <{from_email}>"
        msg["To"]      = to_email
        msg["Subject"] = f"Application for {job_title}" + (f" at {company}" if company else "")

        body = f"{cover_letter}\n\nBest regards,\n{applicant_name}"
        msg.attach(MIMEText(body, "plain"))

        # Attach resume
        if resume_path and os.path.exists(resume_path):
            async with aiofiles.open(resume_path, "rb") as f:
                data = await f.read()
            part = MIMEApplication(data, Name=os.path.basename(resume_path))
            part["Content-Disposition"] = f'attachment; filename="{os.path.basename(resume_path)}"'
            msg.attach(part)
            result.log_step(f"Attached resume: {os.path.basename(resume_path)}")
        else:
            result.log_step("⚠️  Resume file not found — sending without attachment")

        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER,
            password=SMTP_PASS,
            start_tls=True,
        )
        result.success = True
        result.log_step(f"✅ Email sent to {to_email}")

    except Exception as e:
        result.log_step(f"❌ Email failed: {e}")

    return result


# ── PATH B: Form fill via Playwright ─────────────────────────────────────────
# Field guessing heuristics — covers Greenhouse, Lever, Workday, custom forms
FIELD_HINTS = {
    "name":       ["name", "full_name", "fullname", "applicant_name"],
    "email":      ["email", "e-mail", "emailaddress"],
    "phone":      ["phone", "mobile", "contact", "telephone"],
    "linkedin":   ["linkedin", "linked_in", "linkedin_url"],
    "github":     ["github", "github_url"],
    "portfolio":  ["portfolio", "website", "personal_url"],
    "resume":     ["resume", "cv", "upload_resume", "attach_resume"],
    "cover_letter": ["cover", "cover_letter", "coverletter", "message", "motivation"],
}


async def apply_via_form(
    url: str,
    profile: dict,
    cover_letter: str,
    resume_path: str,
) -> ApplyResult:
    result = ApplyResult(success=False, method="form")
    result.log_step(f"Opening: {url}")

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            # Go to apply page
            await page.goto(url, wait_until="networkidle", timeout=30000)
            result.log_step("Page loaded")

            # Detect CAPTCHA before doing anything
            captcha_selectors = [
                "iframe[src*='recaptcha']",
                ".g-recaptcha",
                ".h-captcha",
                "[class*='captcha']",
            ]
            for sel in captcha_selectors:
                if await page.locator(sel).count() > 0:
                    result.captcha_blocked = True
                    result.log_step("🚧 CAPTCHA detected — human intervention needed")
                    await browser.close()
                    return result

            # Fill text inputs
            filled = 0
            inputs = await page.locator("input:visible, textarea:visible").all()
            for inp in inputs:
                name_attr = (await inp.get_attribute("name") or "").lower()
                placeholder = (await inp.get_attribute("placeholder") or "").lower()
                id_attr = (await inp.get_attribute("id") or "").lower()
                combined = f"{name_attr} {placeholder} {id_attr}"

                value = _match_field(combined, profile, cover_letter)
                if value:
                    inp_type = (await inp.get_attribute("type") or "").lower()
                    if inp_type == "file" and resume_path and os.path.exists(resume_path):
                        await inp.set_input_files(resume_path)
                        result.log_step(f"Uploaded resume to file input")
                    elif inp_type not in ("submit", "button", "checkbox", "radio"):
                        await inp.fill(str(value))
                        filled += 1

            result.log_step(f"Filled {filled} fields")

            # Submit
            submit_btn = page.locator(
                "button[type=submit], input[type=submit], button:has-text('Submit'), "
                "button:has-text('Apply'), button:has-text('Send')"
            ).first
            if await submit_btn.count() > 0:
                await submit_btn.click()
                await page.wait_for_load_state("networkidle", timeout=15000)
                result.log_step("✅ Form submitted")
                result.success = True
            else:
                result.log_step("⚠️  Could not find submit button — needs manual review")

            await browser.close()

    except Exception as e:
        result.log_step(f"❌ Form fill error: {e}")

    return result


def _match_field(combined: str, profile: dict, cover_letter: str) -> Optional[str]:
    """Return the right profile value for a field based on name/placeholder heuristics."""
    mapping = {
        "name":         profile.get("full_name", ""),
        "email":        profile.get("email", ""),
        "phone":        profile.get("phone", ""),
        "linkedin":     profile.get("linkedin", ""),
        "github":       profile.get("github", ""),
        "portfolio":    profile.get("portfolio", ""),
        "cover_letter": cover_letter,
    }
    for key, hints in FIELD_HINTS.items():
        if any(h in combined for h in hints):
            return mapping.get(key, "")
    return None


# ── Unified apply entry point ─────────────────────────────────────────────────
async def apply(job: dict, profile: dict, cover_letter: str) -> ApplyResult:
    """
    job dict keys: apply_type, apply_email, apply_url, title, company
    profile dict keys: full_name, email, phone, linkedin, github, portfolio, resume_path
    """
    apply_type = job.get("apply_type", "manual")

    if apply_type == "email" and job.get("apply_email"):
        return await apply_via_email(
            to_email       = job["apply_email"],
            job_title      = job.get("title", "Software Developer"),
            company        = job.get("company", ""),
            cover_letter   = cover_letter,
            resume_path    = profile.get("resume_path", ""),
            applicant_name = profile.get("full_name", ""),
            from_email     = profile.get("email", SMTP_USER),
        )
    elif apply_type == "url" and job.get("apply_url"):
        return await apply_via_form(
            url          = job["apply_url"],
            profile      = profile,
            cover_letter = cover_letter,
            resume_path  = profile.get("resume_path", ""),
        )
    else:
        result = ApplyResult(success=False, method="manual")
        result.log_step("Manual apply required — no email or URL found")
        return result
