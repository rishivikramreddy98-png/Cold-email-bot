import imaplib
import email
import time
import logging
import threading
import requests
from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from models import User, Lead, Subscription
from email_service import check_subscription_limits

logger = logging.getLogger(__name__)

class ReplyChecker(threading.Thread):
    def __init__(self, session_local, telegram_bot_token, check_interval_minutes=5):
        super().__init__()
        self.session_local = session_local
        self.bot_token = telegram_bot_token
        self.check_interval_seconds = check_interval_minutes * 60
        self.stop_event = threading.Event()
        self.daemon = True
        logger.info(f"ReplyChecker initialized with check interval: {check_interval_minutes} minutes")

    def run(self):
        while not self.stop_event.is_set():
            logger.info("ReplyChecker: Starting check cycle...")
            db = self.session_local()
            try:
                users = db.query(User).filter_by(imap_enabled=True).all()
                for user in users:
                    if user.imap_host and user.imap_port and user.smtp_email and user.smtp_password:
                        self._check_inbox(user, db)
            except Exception as e:
                logger.error(f"ReplyChecker error: {e}")
            finally:
                db.close()
            self.stop_event.wait(self.check_interval_seconds)

    def stop(self):
        self.stop_event.set()

    def _send_telegram_notification(self, chat_id, text):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            }, timeout=10)
        except Exception as e:
            logger.error(f"Failed to send notification to {chat_id}: {e}")

    def _check_inbox(self, user: User, db: Session):
        can_detect, message = check_subscription_limits(db, user.id, "reply_detection")
        if not can_detect:
            return
        try:
            mail = imaplib.IMAP4_SSL(user.imap_host, user.imap_port)
            mail.login(user.smtp_email, user.smtp_password)
            mail.select("inbox")

            since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            status, email_ids = mail.search(None, f'(UNSEEN SINCE "{since_date}")')
            email_id_list = email_ids[0].split()

            if not email_id_list:
                mail.logout()
                return

            lead_emails = {lead.email for lead in db.query(Lead).filter_by(user_id=user.id).all()}

            for email_id in email_id_list[-10:]:  # Check last 10
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        sender_email = email.utils.parseaddr(msg["from"])[1]
                        subject = msg["subject"] or "No Subject"

                        if sender_email in lead_emails:
                            lead = db.query(Lead).filter_by(user_id=user.id, email=sender_email).first()
                            lead_name = lead.first_name if lead else sender_email
                            notification = (
                                f"📬 *Reply Received!*\n\n"
                                f"From: {lead_name} ({sender_email})\n"
                                f"Subject: {subject}\n\n"
                                f"Check your inbox for details!"
                            )
                            self._send_telegram_notification(user.telegram_id, notification)
                            mail.store(email_id, "+FLAGS", "\\Seen")
                            logger.info(f"Reply from {sender_email} notified to user {user.telegram_id}")

            mail.logout()
        except Exception as e:
            logger.error(f"IMAP error for user {user.telegram_id}: {e}")
