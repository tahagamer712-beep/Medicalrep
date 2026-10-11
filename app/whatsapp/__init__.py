"""WhatsApp Business Platform (Cloud API) integration — official API only.

No WhatsApp Web automation anywhere in this package. All credentials come
from the environment; nothing secret is hardcoded or logged.
Real network calls happen only through CloudApiWhatsAppSender, which is
never instantiated in tests.
"""
