import imaplib
import email
import time
import logging
import threading
from datetime import datetime, timedelta

from telegram import Bot
from sqlalchemy.orm import Session
from models import User, Lead, init_db

logger = logging.getLogger(__name__)

class ReplyChecker(threading.Thread):
    def __init__(self, session_local, telegram_bot_token, check_interval_minutes=5):
        super().__init__()
        self.session_local = session_local
        self.bot = Bot(telegram_bot_token)
        self.check_interval_seconds = check_interval_minutes * 60
        self.stop_event = threading.Event()
        logger.info(f"ReplyChecker initialized with check interval: {check_interval_minutes} minutes")

    def run(self):
        while not self.stop_event.is_set():
            logger.info("ReplyChecker: Starting a new check cycle...")
            db = self.session_local()
            try:
                users = db.query(User).filter(User.imap_enabled == True).all()
                for user in users:
                    if user.imap_host and user.imap_port and user.smtp_email and user.smtp_password:
                        logger.info(f"Checking inbox for user: {user.telegram_id} ({user.smtp_email})")
                        self._check_inbox(user, db)
                    else:
                        logger.warning(f"User {user.telegram_id} has IMAP enabled but missing credentials.")
            except Exception as e:
                logger.error(f"Error in ReplyChecker run cycle: {e}")
            finally:
                db.close()
            self.stop_event.wait(self.check_interval_seconds)

    def stop(self):
        self.stop_event.set()
        logger.info("ReplyChecker: Stop event set.")

    async def _send_telegram_notification(self, telegram_id, message):
        try:
            await self.bot.send_message(chat_id=telegram_id, text=message)
            logger.info(f"Notification sent to {telegram_id}: {message}")
        except Exception as e:
            logger.error(f"Failed to send Telegram notification to {telegram_id}: {e}")

    def _get_lead_emails(self, user_id, db: Session):
        return {lead.email for lead in db.query(Lead).filter_by(user_id=user_id).all()}

    def _check_inbox(self, user: User, db: Session):
        try:
            mail = imaplib.IMAP4_SSL(user.imap_host, user.imap_port)
            mail.login(user.smtp_email, user.smtp_password)
            mail.select("inbox")

            # Search for emails from the last 24 hours to avoid re-processing old emails repeatedly
            since_date = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            status, email_ids = mail.search(None, "(UNSEEN SINCE \"{since_date}\")")
            email_id_list = email_ids[0].split()

            if not email_id_list:
                logger.info(f"No new unread emails for user {user.telegram_id}.")
                mail.logout()
                return

            lead_emails = self._get_lead_emails(user.id, db)

            for email_id in email_id_list:
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        sender_email = email.utils.parseaddr(msg["from"])[1]
                        subject = msg["subject"]

                        if sender_email in lead_emails:
                            lead = db.query(Lead).filter_by(user_id=user.id, email=sender_email).first()
                            lead_name = lead.first_name if lead else sender_email
                            notification_message = (
                                f"🔔 Reply received from {lead_name} ({sender_email})! "
                                f"Subject: {subject}. Check your inbox."
                            )
                            # Use a new event loop for async operation in a thread
                            import asyncio
                            asyncio.run(self._send_telegram_notification(user.telegram_id, notification_message))
                            
                            # Mark email as seen after processing
                            mail.store(email_id, "+FLAGS", "\\Seen")
                            logger.info(f"Reply detected and notified for user {user.telegram_id} from {sender_email}.")
                        else:
                            # Optionally mark as seen if not a lead reply, or leave unread
                            mail.store(email_id, "+FLAGS", "\\Seen")
                            logger.info(f"Email from {sender_email} is not from a lead for user {user.telegram_id}. Marked as seen.")

            mail.logout()
        except Exception as e:
            logger.error(f"Error checking IMAP for user {user.telegram_id} ({user.smtp_email}): {e}")

if __name__ == '__main__':
    # This part is for testing the reply checker independently
    # In the actual bot, it will be initialized and run as a thread.
    # For testing, you would need to set up a dummy database and a bot token.
    pass
