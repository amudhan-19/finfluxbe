# @file    services/email/__init__.py
#
# Public API for the email service package. Existing import paths
# (`from app.services.email import send_invite_email`) continue to work
# unchanged now that this directory is a package (shadows email.py).

from app.services.email.email_config import (  # noqa: F401
    SMTP_CONFIG_INVALID,
    SMTP_FROM_MISMATCH,
)
from app.services.email.email_service import (  # noqa: F401
    EmailSendResult,
    safe_message_for,
    send_invite_email,
    send_reset_email,
    send_password_changed_email,
    send_email,
    SMTP_CONFIG_MISSING,
    SMTP_HOST_RESOLUTION_FAILED,
    SMTP_AUTH_FAILED,
    SMTP_RECIPIENT_REFUSED,
    SMTP_CONNECTION_FAILED,
    SMTP_SEND_FAILED,
    EMAIL_TEMPLATE_ERROR,
)
