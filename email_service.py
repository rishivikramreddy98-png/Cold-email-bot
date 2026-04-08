import smtplib
import time
import logging
import datetime
import re
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.orm import Session
from models import Lead, Template, Campaign, CampaignLog, User, Subscription

logger = logging.getLogger(__name__)

SUBSCRIPTION_PLANS = {
    "Free": {"price": "0", "emails_day": 50, "max_campaigns": 2, "reply_detection": False},
    "Basic": {"price": "9.99", "emails_day": 300, "max_campaigns": 10, "reply_detection": True},
    "Pro": {"price": "29.99", "emails_day": 1000, "max_campaigns": -1, "reply_detection": True},
}

def validate_email(email_address):
    """Validate email format using regex."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email_address.strip()))

def check_subscription_limits(db: Session, user_id: int, feature: str):
    """Check if user can perform action based on subscription."""
    subscription = db.query(Subscription).filter_by(user_id=user_id).first()
    if not subscription or not subscription.is_active:
        return False, "Your subscription is inactive. Please contact @Rishi1bitcoin to subscribe."
    
    if subscription.end_date and subscription.end_date < datetime.datetime.utcnow():
        subscription.is_active = False
        db.commit()
        return False, "Your subscription has expired. Please contact @Rishi1bitcoin to renew."

    if feature == "email_send":
        if subscription.daily_limit != -1 and subscription.emails_sent_today >= subscription.daily_limit:
            return False, f"Daily email limit ({subscription.daily_limit}) reached. Upgrade your plan or wait until tomorrow."
    elif feature == "campaign_creation":
        if subscription.max_campaigns != -1:
            current_campaigns = db.query(Campaign).filter_by(user_id=user_id, status="running").count()
            if current_campaigns >= subscription.max_campaigns:
                return False, f"Max active campaigns ({subscription.max_campaigns}) reached. Upgrade or complete existing campaigns."
    elif feature == "reply_detection":
        plan_info = SUBSCRIPTION_PLANS.get(subscription.plan_name, {})
        if not plan_info.get("reply_detection", False):
            return False, "Reply detection not available in your plan. Upgrade to Basic or Pro."
    
    return True, ""

def test_smtp_connection(email, password, smtp_host, smtp_port):
    """Test SMTP connection before starting campaigns."""
    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(email, password)
        server.quit()
        logger.info(f"SMTP test successful for {email}")
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"Authentication failed: {str(e)}. For Gmail, use an App Password."
        logger.error(f"SMTP auth error for {email}: {error_msg}")
        return False, error_msg
    except smtplib.SMTPConnectError as e:
        error_msg = f"Connection failed: {str(e)}"
        logger.error(f"SMTP connect error for {email}: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"SMTP error: {str(e)}"
        logger.error(f"SMTP test error for {email}: {error_msg}")
        return False, error_msg

def send_email(user_email, user_password, smtp_host, smtp_port, to_email, subject, body):
    """Send a single email via SMTP with TLS."""
    try:
        if not validate_email(to_email):
            return False, f"Invalid email address: {to_email}"
        
        msg = MIMEMultipart()
        msg["From"] = user_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(user_email, user_password)
        server.send_message(msg)
        server.quit()
        logger.info(f"Email sent to {to_email} from {user_email}")
        return True, None
    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"Auth failed: {str(e)}"
        logger.error(f"SMTP auth error sending to {to_email}: {error_msg}")
        return False, error_msg
    except smtplib.SMTPRecipientsRefused as e:
        error_msg = f"Recipient refused: {str(e)}"
        logger.error(f"Recipient refused {to_email}: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = f"Send failed: {str(e)}"
        logger.error(f"Error sending to {to_email}: {error_msg}")
        return False, error_msg

def personalize_template(template_text, lead):
    """Replace placeholders with lead data."""
    replacements = {
        "{first_name}": lead.first_name or "",
        "{last_name}": getattr(lead, 'last_name', '') or "",
        "{company}": lead.company or "",
        "{email}": lead.email or "",
        "{industry}": getattr(lead, 'industry', '') or "",
        "{location}": getattr(lead, 'location', '') or "",
    }
    result = template_text
    for key, value in replacements.items():
        result = result.replace(key, str(value))
    return result

def run_campaign(db: Session, campaign_id: int, delay_seconds: int = 15):
    """Run a campaign with delays and error handling."""
    campaign = db.query(Campaign).filter_by(id=campaign_id).first()
    if not campaign:
        logger.error(f"Campaign {campaign_id} not found.")
        return

    user = db.query(User).filter_by(id=campaign.user_id).first()
    if not user or not user.smtp_email or not user.smtp_password:
        logger.error(f"User or SMTP credentials missing for campaign {campaign_id}.")
        campaign.status = "failed"
        db.commit()
        return

    template = db.query(Template).filter_by(id=campaign.template_id, user_id=user.id).first()
    if not template:
        logger.error(f"Template not found for campaign {campaign_id}.")
        campaign.status = "failed"
        db.commit()
        return

    # Test SMTP first
    success, error = test_smtp_connection(user.smtp_email, user.smtp_password, user.smtp_host, user.smtp_port)
    if not success:
        logger.error(f"SMTP test failed for campaign {campaign_id}: {error}")
        campaign.status = "failed"
        db.commit()
        return

    leads = db.query(Lead).filter_by(user_id=user.id).all()
    campaign.status = "running"
    db.commit()

    subscription = db.query(Subscription).filter_by(user_id=user.id).first()
    if not subscription:
        campaign.status = "failed"
        db.commit()
        return

    for lead in leads:
        # Check if campaign was paused or cancelled
        db.refresh(campaign)
        if campaign.status in ("paused", "cancelled"):
            logger.info(f"Campaign {campaign_id} {campaign.status}. Stopping.")
            return

        # Skip if already sent
        if db.query(CampaignLog).filter_by(campaign_id=campaign_id, lead_id=lead.id).first():
            continue

        # Check email limit
        can_send, limit_msg = check_subscription_limits(db, user.id, "email_send")
        if not can_send:
            logger.warning(f"Campaign {campaign_id}: Limit reached - {limit_msg}")
            campaign.status = "paused"
            db.commit()
            return

        # Validate email
        if not validate_email(lead.email):
            log = CampaignLog(user_id=user.id, campaign_id=campaign_id, lead_id=lead.id,
                            status="failed", error_message="Invalid email address",
                            follow_up_stage=campaign.follow_up_stage)
            db.add(log)
            db.commit()
            continue

        # Personalize and send
        personalized_subject = personalize_template(template.subject, lead)
        personalized_body = personalize_template(template.body, lead)
        
        success, error = send_email(
            user.smtp_email, user.smtp_password, user.smtp_host, user.smtp_port,
            lead.email, personalized_subject, personalized_body
        )

        # Update subscription counter
        subscription.emails_sent_today += 1
        
        log = CampaignLog(
            user_id=user.id, campaign_id=campaign_id, lead_id=lead.id,
            status="sent" if success else "failed",
            error_message=error, follow_up_stage=campaign.follow_up_stage
        )
        db.add(log)
        db.commit()

        if success:
            logger.info(f"Campaign {campaign_id}: Sent to {lead.email}")
        else:
            logger.error(f"Campaign {campaign_id}: Failed to {lead.email} - {error}")

        # Random delay between 10-20 seconds
        delay = random.randint(10, 20)
        time.sleep(delay)

    campaign.status = "completed"
    db.commit()
    logger.info(f"Campaign {campaign_id} completed.")
