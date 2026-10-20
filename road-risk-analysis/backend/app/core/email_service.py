"""
Outgoing e-mail over SMTP, configured at runtime from Settings → Platform →
E-mail. Defaults target Gmail (smtp.gmail.com:587, STARTTLS) with an App
Password. smtplib is blocking, so sends run in a worker thread.
"""
import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_service import SmtpSettings, get_section, smtp_configured

logger = logging.getLogger(__name__)


class EmailNotConfigured(Exception):
    pass


class EmailSendError(Exception):
    pass


def _send_blocking(cfg: SmtpSettings, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if cfg.security == "ssl":
        server = smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=20)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=20)
    try:
        if cfg.security == "starttls":
            server.starttls(context=ctx)
        if cfg.username:
            server.login(cfg.username, cfg.password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass


async def send_email(db: AsyncSession, to: str, subject: str, text: str, html: str | None = None) -> None:
    cfg: SmtpSettings = await get_section(db, "smtp")  # type: ignore[assignment]
    if not smtp_configured(cfg):
        raise EmailNotConfigured("SMTP is not configured (Settings → Platform → E-mail).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.from_name, cfg.from_email or cfg.username))
    msg["To"] = to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        await asyncio.to_thread(_send_blocking, cfg, msg)
    except (smtplib.SMTPException, OSError) as exc:
        logger.warning("E-mail to %s failed: %s", to, exc)
        raise EmailSendError(str(exc)) from exc


# ── Templates (sent in the recipient's language) ─────────────────────────────

_RESET = {
    "en": ("Reset your VIASafe password",
           "Hello {name},\n\nA password reset was requested for your VIASafe account.\n"
           "Open this link within {minutes} minutes to choose a new password:\n\n{link}\n\n"
           "If you did not request this, you can ignore this e-mail."),
    "fr": ("Réinitialisation de votre mot de passe VIASafe",
           "Bonjour {name},\n\nUne réinitialisation du mot de passe de votre compte VIASafe a été demandée.\n"
           "Ouvrez ce lien dans les {minutes} minutes pour choisir un nouveau mot de passe :\n\n{link}\n\n"
           "Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail."),
    "es": ("Restablezca su contraseña de VIASafe",
           "Hola {name},\n\nSe solicitó restablecer la contraseña de su cuenta VIASafe.\n"
           "Abra este enlace en los próximos {minutes} minutos para elegir una nueva contraseña:\n\n{link}\n\n"
           "Si no realizó esta solicitud, puede ignorar este correo."),
}

_INVITE = {
    "en": ("Your VIASafe account",
           "Hello {name},\n\nAn account was created for you on the VIASafe platform.\n"
           "Open this link within {minutes} minutes to set your password:\n\n{link}\n"),
    "fr": ("Votre compte VIASafe",
           "Bonjour {name},\n\nUn compte a été créé pour vous sur la plateforme VIASafe.\n"
           "Ouvrez ce lien dans les {minutes} minutes pour définir votre mot de passe :\n\n{link}\n"),
    "es": ("Su cuenta VIASafe",
           "Hola {name},\n\nSe creó una cuenta para usted en la plataforma VIASafe.\n"
           "Abra este enlace en los próximos {minutes} minutos para definir su contraseña:\n\n{link}\n"),
}


def render_reset(lang: str, invite: bool, **kw) -> tuple[str, str]:
    table = _INVITE if invite else _RESET
    subject, body = table.get(lang, table["en"])
    return subject, body.format(**kw)
