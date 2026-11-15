# @file    services/email/email_templates.py
#
# HTML and plain-text email templates for FinFlux transactional emails.
# All template functions return (subject, html_body, text_body).
# No credentials, tokens, or PII are logged here — callers handle logging.

from __future__ import annotations


# ── Shared layout helpers ────────────────────────────────────────────────────

def _html_wrap(body_html: str) -> str:
    """Wrap body content in the FinFlux branded email shell."""
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "</head>"
        '<body style="margin:0;padding:0;background-color:#f1f5f9;'
        "font-family:Arial,'Helvetica Neue',Helvetica,sans-serif\">"
        '<table role="presentation" cellpadding="0" cellspacing="0"'
        ' border="0" width="100%" style="background-color:#f1f5f9">'
        "<tr><td align=\"center\" style=\"padding:40px 16px\">"
        "<!--[if mso]><table role=\"presentation\" width=\"560\">"
        "<tr><td><![endif]-->"
        '<table role="presentation" cellpadding="0" cellspacing="0"'
        ' border="0" width="560"'
        ' style="max-width:560px;width:100%;background-color:#ffffff;'
        "border-radius:8px;border:1px solid #e2e8f0;overflow:hidden\">"
        # Header
        "<tr><td style=\"background-color:#16a34a;padding:24px 32px\">"
        '<span style="font-size:20px;font-weight:900;color:#ffffff;'
        'letter-spacing:-0.5px">Tick<em style="font-style:italic">&amp;</em>Tie</span>'
        '<span style="font-size:13px;color:rgba(255,255,255,0.85);'
        'margin-left:8px;font-weight:400">FinFlux</span>'
        "</td></tr>"
        # Body
        '<tr><td style="padding:32px">'
        + body_html
        + "</td></tr>"
        # Footer
        '<tr><td style="padding:16px 32px;background-color:#f8fafc;'
        'border-top:1px solid #e2e8f0">'
        '<p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.5">'
        "This email was sent by FinFlux. If you did not expect this,"
        " you can safely ignore it."
        "</p></td></tr>"
        "</table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table>"
        "</body></html>"
    )


def _cta_button(href: str, label: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' style="margin:24px 0">'
        "<tr><td style=\"background-color:#16a34a;border-radius:6px;"
        'padding:12px 28px;text-align:center">'
        f'<a href="{href}" target="_blank"'
        ' style="color:#ffffff;text-decoration:none;font-weight:700;'
        f'font-size:14px;display:inline-block">{label}</a>'
        "</td></tr></table>"
    )


def _fallback_link_block(href: str, label: str) -> str:
    return (
        '<p style="margin:8px 0 4px;font-size:12px;color:#64748b">'
        "If the button above does not work, copy and paste this link into your browser:"
        "</p>"
        f'<p style="margin:0;font-size:12px;word-break:break-all">'
        f'<a href="{href}" style="color:#2563eb">{href}</a></p>'
    )


def _p(text: str, size: int = 14, color: str = "#334155") -> str:
    return (
        f'<p style="margin:0 0 14px;font-size:{size}px;'
        f'color:{color};line-height:1.6">{text}</p>'
    )


# ── Invite / Welcome email ───────────────────────────────────────────────────

def build_invite_email(
    first_name: str,
    invite_link: str,
    expire_hours: int,
) -> tuple[str, str, str]:
    """Welcome / account activation email sent when Admin creates a user.

    Returns (subject, html_body, text_body).
    """
    subject = "Welcome to FinFlux — Activate your account"

    body_html = (
        _p(f"Hi {first_name},")
        + _p(
            "Your FinFlux account has been created. Click the button below to "
            "set your password and activate your account."
        )
        + _cta_button(invite_link, "Activate account")
        + _fallback_link_block(invite_link, "Activate account")
        + _p(
            f"This link expires in <strong>{expire_hours} hours</strong>. "
            "If you were not expecting this invitation, you can safely ignore this email.",
            size=13,
            color="#64748b",
        )
        + _p("— The FinFlux Team", size=13, color="#64748b")
    )

    text_body = (
        f"Hi {first_name},\n\n"
        "Your FinFlux account has been created.\n\n"
        f"Activate your account and set a password:\n{invite_link}\n\n"
        f"This link expires in {expire_hours} hours.\n\n"
        "If you were not expecting this invitation, please ignore this email.\n\n"
        "— The FinFlux Team"
    )

    return subject, _html_wrap(body_html), text_body


# ── Forgot password / Reset email ────────────────────────────────────────────

def build_reset_email(
    first_name: str,
    reset_link: str,
    expire_hours: int,
) -> tuple[str, str, str]:
    """Password reset email triggered by /forgot-password.

    Returns (subject, html_body, text_body).
    """
    subject = "FinFlux — Password reset request"

    body_html = (
        _p(f"Hi {first_name},")
        + _p(
            "We received a request to reset the password for your FinFlux account. "
            "Click the button below to set a new password."
        )
        + _cta_button(reset_link, "Reset password")
        + _fallback_link_block(reset_link, "Reset password")
        + _p(
            f"This link expires in <strong>{expire_hours} hours</strong>. "
            "If you did not request a password reset, "
            "you can safely ignore this email — your password will not change.",
            size=13,
            color="#64748b",
        )
        + _p("— The FinFlux Team", size=13, color="#64748b")
    )

    text_body = (
        f"Hi {first_name},\n\n"
        "A password reset was requested for your FinFlux account.\n\n"
        f"Reset your password here:\n{reset_link}\n\n"
        f"This link expires in {expire_hours} hours.\n\n"
        "If you did not request this, please ignore this email.\n\n"
        "— The FinFlux Team"
    )

    return subject, _html_wrap(body_html), text_body


# ── Password changed confirmation (optional) ─────────────────────────────────

def build_password_changed_email(first_name: str) -> tuple[str, str, str]:
    """Confirmation sent after a successful password reset.

    Returns (subject, html_body, text_body).
    """
    subject = "FinFlux — Your password was changed"

    body_html = (
        _p(f"Hi {first_name},")
        + _p(
            "This is a confirmation that the password for your FinFlux account "
            "was recently changed."
        )
        + _p(
            "If you made this change, no further action is required. "
            "If you did not change your password, please contact your FinFlux administrator immediately.",
            size=13,
            color="#64748b",
        )
        + _p("— The FinFlux Team", size=13, color="#64748b")
    )

    text_body = (
        f"Hi {first_name},\n\n"
        "Your FinFlux account password was recently changed.\n\n"
        "If you made this change, no further action is required.\n"
        "If you did not change your password, contact your FinFlux administrator immediately.\n\n"
        "— The FinFlux Team"
    )

    return subject, _html_wrap(body_html), text_body
