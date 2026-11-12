"""Email: the only module that talks to Resend. One message type, the class token link."""

import httpx

from app.config import get_settings
from app.thresholds import TOKEN_LINK_TTL_SECONDS

RESEND_URL = "https://api.resend.com/emails"


def token_link_email(link: str) -> dict:
    """Subject, plain text and HTML for the token link. Plain wording keeps it out of spam."""
    settings = get_settings()
    minutes = TOKEN_LINK_TTL_SECONDS // 60
    name = f"{settings.clinic_name} Assistant"
    text = (
        f"Someone asked for a class API token for this address on the {name} site.\n\n"
        f"Open this link to see your token. It works once and expires in {minutes} minutes:\n"
        f"{link}\n\n"
        "If you did not ask for this, ignore this email. No token exists until the link is opened."
    )
    html = (
        f"<p>Someone asked for a class API token for this address on the {name} site.</p>"
        f'<p><a href="{link}">Show my class token</a></p>'
        f"<p>The link works once and expires in {minutes} minutes.</p>"
        "<p>If you did not ask for this, ignore this email. "
        "No token exists until the link is opened.</p>"
    )
    return {"subject": f"Your class token link for the {name}", "text": text, "html": html}


async def send_token_link(to: str, link: str) -> None:
    """Send the one-time link. Raises if Resend does not accept the message."""
    settings = get_settings()
    if not settings.resend_api_key or not settings.email_from:
        raise RuntimeError("RESEND_API_KEY and EMAIL_FROM must be set to send email")
    message = {"from": settings.email_from, "to": [to], **token_link_email(link)}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            RESEND_URL,
            json=message,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
        )
        response.raise_for_status()
